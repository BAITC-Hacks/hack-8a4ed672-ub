"""WebSocket endpoint of protocol hattama.audio.v1 (see packages/contracts/audio-frame-v1.md).

One IngestConnection per client. Backpressure: frames are processed inline in the receive loop, so a slow
disk slows reading from the socket (TCP flow control) instead of growing an unbounded queue.
ACK is sent only after fsync (ack_level = "fsync").
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anyio
import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from hattama_contracts.frame import FrameError, decode_frame
from hattama_contracts.messages import (
    Ack,
    ErrorMsg,
    GapRecorded,
    GapReport,
    Hello,
    Limits,
    Ping,
    Pong,
    SessionStopped,
    SourceClose,
    SourceClosed,
    SourceOpen,
    SourceReady,
    SourceState,
    StopRequested,
    StopSession,
    Welcome,
    client_message_adapter,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from hattama.config import Settings, get_settings
from hattama.db.models import CaptureSession
from hattama.db.session import session_scope
from hattama.domain.enums import CaptureState
from hattama.ingest import service
from hattama.ingest.service import IngestAuth, IngestError, OpenedSource
from hattama.ingest.store import EpochWriter, SequenceTracker
from hattama.security.origins import ingest_origin_kind
from hattama.services.events import publish

log = logging.getLogger(__name__)
T = TypeVar("T")
SESSION_COOKIE = "hattama_session"
HELLO_TIMEOUT_S = 10.0
MAX_BAD_FRAMES = 3
SILENCE_DBFS = -55.0
SILENCE_AFTER_S = 10.0
# a peer that vanished may surface as any of these, depending on the ASGI server
_SEND_ERRORS = (RuntimeError, WebSocketDisconnect, anyio.ClosedResourceError, anyio.BrokenResourceError, OSError)


async def run_db(fn: Callable[..., T], *args: Any) -> T:
    def _call() -> T:
        with session_scope() as session:
            return fn(session, *args)

    return await anyio.to_thread.run_sync(_call)


@dataclass
class SourceRuntime:
    opened: OpenedSource
    writer: EpochWriter
    tracker: SequenceTracker
    closed: bool = False
    dirty: bool = False
    acked_seq: int = -1
    acked_end: int = 0
    level_sum_sq: float = 0.0
    level_count: int = 0
    level_peak: float = 0.0
    silence_since: float | None = None
    silent_state_sent: bool = False
    persisted_seq: int = -2
    last_level_pub: float = field(default_factory=time.monotonic)


class ConnectionRegistry:
    """Single API process owns ingestion (documented constraint). New connection supersedes the old one."""

    def __init__(self) -> None:
        self._by_session: dict[str, IngestConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, capture_session_id: str, conn: IngestConnection) -> None:
        async with self._lock:
            old = self._by_session.get(capture_session_id)
            self._by_session[capture_session_id] = conn
        if old is not None and old is not conn:
            await old.supersede()

    def unregister(self, capture_session_id: str, conn: IngestConnection) -> None:
        if self._by_session.get(capture_session_id) is conn:
            del self._by_session[capture_session_id]

    def get(self, capture_session_id: str) -> IngestConnection | None:
        return self._by_session.get(capture_session_id)

    def active_ids(self) -> list[str]:
        return list(self._by_session)


registry = ConnectionRegistry()


class IngestConnection:
    def __init__(self, ws: WebSocket, settings: Settings, origin_kind: str) -> None:
        self.ws = ws
        self.settings = settings
        self.origin_kind = origin_kind
        self.auth: IngestAuth | None = None
        self.sources: dict[int, SourceRuntime] = {}
        self.closed_sources: dict[tuple[int, int], int] = {}
        self.bad_frames = 0
        self.stop_sent = False
        self.closing = False
        self.finished = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._last_persist = 0.0
        self._last_state_check = 0.0

    # ------------------------------------------------------------------ plumbing
    async def send(self, msg: Any) -> None:
        if self.closing:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(msg.model_dump_json())
            except _SEND_ERRORS:
                self.closing = True

    async def supersede(self) -> None:
        await self.send(ErrorMsg(code="superseded", message="Открыто новое соединение для этой сессии", fatal=True))
        self.closing = True
        try:
            await self.ws.close(code=4409)
        except _SEND_ERRORS:
            pass
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=5)
        except TimeoutError:
            log.warning("superseded connection did not finish in time")

    async def fatal(self, code: str, message: str, close_code: int = 4400) -> None:
        await self.send(ErrorMsg(code=code, message=message, fatal=True))
        self.closing = True
        try:
            await self.ws.close(code=close_code)
        except _SEND_ERRORS:
            pass

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        try:
            if not await self._handshake():
                return
            ticker = asyncio.create_task(self._ticker())
            try:
                while not self.closing:
                    message = await self.ws.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        await self._on_frame(message["bytes"])
                    elif message.get("text") is not None:
                        await self._on_text(message["text"])
            finally:
                ticker.cancel()
                with _suppress_cancel():
                    await ticker
        except WebSocketDisconnect:
            pass
        finally:
            # ASGI/TestClient cancels the connection task after disconnect. Durable checkpoints
            # and registry cleanup must survive that cancellation before another socket resumes.
            with anyio.CancelScope(shield=True):
                await self._cleanup()

    async def _handshake(self) -> bool:
        try:
            raw = await asyncio.wait_for(self.ws.receive_text(), timeout=HELLO_TIMEOUT_S)
            hello = Hello.model_validate_json(raw)
        except TimeoutError:
            await self.fatal("hello_timeout", "Не получено сообщение hello", 4408)
            return False
        except (ValidationError, ValueError, KeyError):
            await self.fatal("bad_hello", "Некорректное сообщение hello")
            return False
        cookie = self.ws.cookies.get(SESSION_COOKIE)
        try:
            self.auth = await run_db(service.authenticate_hello, hello, self.origin_kind, cookie)
        except IngestError as exc:
            await self.fatal(exc.code, str(exc), 4401 if exc.code == "unauthorized" else 4403)
            return False
        await registry.register(self.auth.capture_session_id, self)
        client = hello.client.model_dump()
        await run_db(service.mark_connected, self.auth, client)
        await self.send(Welcome(
            capture_session_id=self.auth.capture_session_id, meeting_id=self.auth.meeting_id,
            server_time_us=int(time.time() * 1_000_000),
            limits=Limits(max_frame_bytes=self.settings.max_frame_bytes,
                          max_sources=self.settings.max_sources_per_session, ack_level="fsync",
                          reorder_window_frames=self.settings.reorder_window_frames,
                          stop_grace_s=self.settings.stop_grace_s),
        ))
        log.info("ingest connected", extra={"capture_session": self.auth.capture_session_id,
                                            "actor": self.auth.actor_kind})
        return True

    async def _cleanup(self) -> None:
        for src in self.sources.values():
            try:
                await anyio.to_thread.run_sync(src.writer.close)
            except OSError:
                log.exception("failed to close epoch writer")
        if self.auth is not None:
            await self._persist_all(force=True)
            is_current = registry.get(self.auth.capture_session_id) is self
            registry.unregister(self.auth.capture_session_id, self)
            if is_current:
                await run_db(service.mark_disconnected, self.auth, "closed")
        self.finished.set()

    # ------------------------------------------------------------------ messages
    async def _on_text(self, text: str) -> None:
        if len(text) > 16_384:
            await self.fatal("message_too_large", "Слишком большое текстовое сообщение")
            return
        try:
            msg = client_message_adapter.validate_python(json.loads(text))
        except (ValidationError, ValueError):
            await self.send(ErrorMsg(code="bad_message", message="Некорректное сообщение"))
            return
        if isinstance(msg, SourceOpen):
            await self._on_source_open(msg)
        elif isinstance(msg, SourceClose):
            await self._on_source_close(msg)
        elif isinstance(msg, GapReport):
            await self._on_gap_report(msg)
        elif isinstance(msg, SourceState):
            await self._on_source_state(msg)
        elif isinstance(msg, StopSession):
            assert self.auth is not None
            await run_db(service.request_stop, self.auth.capture_session_id, msg.reason, self.auth.user_id,
                         self.auth.actor_kind)
            await self._maybe_finish_stop()
        elif isinstance(msg, Ping):
            await self.send(Pong(t=msg.t, server_time_us=int(time.time() * 1_000_000)))
        elif isinstance(msg, Hello):
            await self.send(ErrorMsg(code="duplicate_hello", message="hello уже получен"))

    async def _on_source_open(self, msg: SourceOpen) -> None:
        assert self.auth is not None
        try:
            opened = await run_db(service.open_source, self.auth, msg, self.settings.max_sources_per_session)
        except IngestError as exc:
            await self.send(ErrorMsg(code=exc.code, message=str(exc), fatal=exc.fatal))
            return
        if opened.closed:
            self.closed_sources[(opened.source_index, msg.capture_epoch)] = opened.durable_sequence
            await self.send(SourceReady(source_id=msg.source_id, source_index=opened.source_index,
                                        capture_epoch=msg.capture_epoch,
                                        resume_from_sequence=opened.durable_sequence + 1,
                                        durable_sequence=opened.durable_sequence))
            await self.send(SourceClosed(source_index=opened.source_index, capture_epoch=msg.capture_epoch,
                                         durable_sequence=opened.durable_sequence))
            await self._maybe_finish_stop()
            return
        existing = self.sources.get(opened.source_index)
        if existing is not None and existing.opened.epoch_id != opened.epoch_id:
            await anyio.to_thread.run_sync(existing.writer.close)
        if existing is None or existing.opened.epoch_id != opened.epoch_id:
            writer = await anyio.to_thread.run_sync(
                lambda: EpochWriter(self.settings.data_dir / opened.rel_path, opened.channel_count))
            tracker = SequenceTracker(opened.durable_sequence, opened.durable_samples,
                                      self.settings.reorder_window_frames, self.settings.reorder_timeout_s)
            self.sources[opened.source_index] = SourceRuntime(opened, writer, tracker,
                                                              acked_seq=opened.durable_sequence,
                                                              acked_end=opened.durable_samples,
                                                              persisted_seq=opened.durable_sequence)
        await self.send(SourceReady(source_id=msg.source_id, source_index=opened.source_index,
                                    capture_epoch=msg.capture_epoch,
                                    resume_from_sequence=opened.durable_sequence + 1,
                                    durable_sequence=opened.durable_sequence))

    async def _on_frame(self, data: bytes) -> None:
        assert self.auth is not None
        try:
            frame = decode_frame(data, max_frame_bytes=self.settings.max_frame_bytes)
        except FrameError as exc:
            self.bad_frames += 1
            if self.bad_frames > MAX_BAD_FRAMES:
                await self.fatal("bad_frame", f"Повторные ошибки формата кадра: {exc.code}")
            else:
                await self.send(ErrorMsg(code="bad_frame", message=f"{exc.code}: {exc}"))
            return
        src = self.sources.get(frame.source_index)
        if src is None:
            await self.send(ErrorMsg(code="unknown_source", message="Кадр для неоткрытого источника"))
            return
        op = src.opened
        if (frame.capture_epoch != self._epoch_number(src) or frame.sample_rate != op.sample_rate
                or frame.channel_count != op.channel_count):
            await self.send(ErrorMsg(code="frame_mismatch", message="Эпоха/формат кадра не совпадает с source_open"))
            return
        if src.closed:
            await self.send(ErrorMsg(code="source_closed", message="Источник закрыт"))
            return
        tracker = src.tracker
        if tracker.is_duplicate(frame.sequence):
            tracker.duplicates += 1
            if tracker.in_declared_gap(frame.sequence) is not None:
                await anyio.to_thread.run_sync(src.writer.write, frame)
                src.dirty = True
                await run_db(service.narrow_gap, op.epoch_id, frame.start_sample, frame.end_sample)
            return
        await anyio.to_thread.run_sync(src.writer.write, frame)
        src.dirty = True
        gaps = tracker.accept(frame.sequence, frame.start_sample, frame.sample_count, time.monotonic())
        await self._record_gaps(src, gaps)
        self._meter(src, frame.payload, frame.channel_count)

    def _epoch_number(self, src: SourceRuntime) -> int:
        # epoch number is encoded in the relative path (…/epoch-<n>.pcm)
        return int(src.opened.rel_path.rsplit("epoch-", 1)[1].split(".", 1)[0])

    async def _record_gaps(self, src: SourceRuntime, gaps: list) -> None:
        assert self.auth is not None
        for gap in gaps:
            await run_db(service.record_gap, self.auth.meeting_id, src.opened.epoch_id, src.opened.source_key,
                         gap.start_sample, gap.end_sample, gap.reason, src.opened.sample_rate)
            await self.send(GapRecorded(source_index=src.opened.source_index, capture_epoch=self._epoch_number(src),
                                        start_sample=gap.start_sample, end_sample=gap.end_sample, reason=gap.reason))

    async def _on_gap_report(self, msg: GapReport) -> None:
        src = self.sources.get(msg.source_index)
        if src is None or msg.capture_epoch != self._epoch_number(src):
            await self.send(ErrorMsg(code="unknown_source", message="gap_report для неизвестного источника"))
            return
        gaps = src.tracker.report_lost(msg.from_sequence, msg.to_sequence, msg.start_sample, msg.lost_samples,
                                       time.monotonic())
        await self._record_gaps(src, gaps)

    async def _on_source_state(self, msg: SourceState) -> None:
        assert self.auth is not None
        await run_db(service.set_source_state, self.auth, msg.source_index, msg.state)

    async def _on_source_close(self, msg: SourceClose) -> None:
        assert self.auth is not None
        closed_sequence = self.closed_sources.get((msg.source_index, msg.capture_epoch))
        if closed_sequence is not None:
            await self.send(SourceClosed(source_index=msg.source_index, capture_epoch=msg.capture_epoch,
                                         durable_sequence=closed_sequence))
            await self._maybe_finish_stop()
            return
        src = self.sources.get(msg.source_index)
        if src is None or msg.capture_epoch != self._epoch_number(src):
            await self.send(ErrorMsg(code="unknown_source", message="source_close для неизвестного источника"))
            return
        if src.tracker.contiguous_seq < msg.final_sequence:
            # frames that never arrived before close: declare them lost rather than wait forever
            gaps = src.tracker.flush_timeouts(time.monotonic() + 3600)
            await self._record_gaps(src, gaps)
            if src.tracker.contiguous_seq < msg.final_sequence:
                last = src.tracker.contiguous_end
                gaps = src.tracker.report_lost(src.tracker.contiguous_seq + 1, msg.final_sequence, last, 0,
                                               time.monotonic())
                for gap in gaps:
                    gap.reason = "missing_at_close"
                await self._record_gaps(src, gaps)
        await anyio.to_thread.run_sync(src.writer.sync)
        await self._ack(src)
        src.closed = True
        await self._persist_all(force=True)
        await run_db(service.close_epoch, src.opened.epoch_id, msg.final_sequence, msg.reason)
        self.closed_sources[(msg.source_index, msg.capture_epoch)] = src.tracker.contiguous_seq
        await anyio.to_thread.run_sync(src.writer.close)
        await run_db(_publish_source_closed, self.auth.meeting_id, src.opened.source_key, msg.reason)
        await self.send(SourceClosed(source_index=msg.source_index, capture_epoch=msg.capture_epoch,
                                     durable_sequence=src.tracker.contiguous_seq))
        await self._maybe_finish_stop()

    async def _maybe_finish_stop(self) -> None:
        assert self.auth is not None
        stopped = await run_db(service.finalize_stop_if_ready, self.auth.capture_session_id,
                               self.settings.stop_grace_s, False)
        if stopped:
            await self.send(SessionStopped(capture_session_id=self.auth.capture_session_id))
            self.closing = True
            try:
                await self.ws.close(code=1000)
            except _SEND_ERRORS:
                pass

    # ------------------------------------------------------------------ periodic work
    async def _ack(self, src: SourceRuntime) -> None:
        seq, end = src.tracker.contiguous_seq, src.tracker.contiguous_end
        if seq > src.acked_seq:
            src.acked_seq, src.acked_end = seq, end
            await self._persist_all()
            await self.send(Ack(source_index=src.opened.source_index, capture_epoch=self._epoch_number(src),
                                durable_sequence=seq, durable_sample=end))

    async def _ticker(self) -> None:
        interval = self.settings.fsync_interval_s
        while not self.closing:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for src in list(self.sources.values()):
                if src.closed:
                    continue
                gaps = src.tracker.flush_timeouts(now)
                await self._record_gaps(src, gaps)
                if src.dirty:
                    watermark = (src.tracker.contiguous_seq, src.tracker.contiguous_end)
                    src.dirty = False
                    await anyio.to_thread.run_sync(src.writer.sync)
                    if watermark[0] > src.acked_seq:
                        src.acked_seq, src.acked_end = watermark
                        # The reconnect checkpoint is part of a durable ACK, not a delayed metric.
                        await self._persist_all()
                        await self.send(Ack(source_index=src.opened.source_index,
                                            capture_epoch=self._epoch_number(src),
                                            durable_sequence=watermark[0], durable_sample=watermark[1]))
            if now - self._last_persist >= 0.5:
                await self._persist_all()
            if now - self._last_state_check >= 1.0:
                self._last_state_check = now
                await self._check_session_state()

    async def _persist_all(self, force: bool = False) -> None:
        assert self.auth is not None
        self._last_persist = time.monotonic()
        updates = []
        levels = []
        now = time.monotonic()
        for src in self.sources.values():
            if force or src.acked_seq != src.persisted_seq:
                updates.append((src.opened.epoch_id, src.acked_seq, src.acked_end, src.tracker.received,
                                src.tracker.duplicates))
                src.persisted_seq = src.acked_seq
            if src.level_count and now - src.last_level_pub >= 0.5:
                levels.append(self._level_payload(src, now))
        if not updates and not levels:
            return
        auth = self.auth

        def _write(session: Session) -> None:
            for epoch_id, seq, end, received, dup in updates:
                service.persist_progress(session, epoch_id, seq, end, received, dup, auth.capture_session_id)
            for payload in levels:
                publish(session, auth.meeting_id, "source.level", payload)
                if payload.get("state_change"):
                    publish(session, auth.meeting_id, "source.state",
                            {"source_id": payload["source_id"], "state": payload["state_change"]})

        await run_db(_write)

    def _meter(self, src: SourceRuntime, payload: bytes, channels: int) -> None:
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0:
            return
        src.level_sum_sq += float(np.dot(samples, samples)) / channels
        src.level_count += samples.size // channels
        src.level_peak = max(src.level_peak, float(np.max(np.abs(samples))))

    def _level_payload(self, src: SourceRuntime, now: float) -> dict[str, Any]:
        rms = math.sqrt(src.level_sum_sq / max(src.level_count, 1))
        rms_db = 20 * math.log10(max(rms, 1e-6))
        peak_db = 20 * math.log10(max(src.level_peak, 1e-6))
        state_change = None
        if rms_db < SILENCE_DBFS:
            src.silence_since = src.silence_since or now
            if now - src.silence_since >= SILENCE_AFTER_S and not src.silent_state_sent:
                src.silent_state_sent = True
                state_change = "silence"
        else:
            if src.silent_state_sent:
                state_change = "signal"
            src.silence_since = None
            src.silent_state_sent = False
        src.level_sum_sq, src.level_count, src.level_peak = 0.0, 0, 0.0
        src.last_level_pub = now
        return {"source_id": src.opened.source_key, "kind": src.opened.kind, "rms_dbfs": round(rms_db, 1),
                "peak_dbfs": round(peak_db, 1), "durable_seconds": round(src.acked_end / src.opened.sample_rate, 2),
                "state_change": state_change}

    async def _check_session_state(self) -> None:
        assert self.auth is not None
        state = await run_db(_capture_state, self.auth.capture_session_id)
        if state == CaptureState.STOPPING:
            if not self.stop_sent:
                self.stop_sent = True
                await self.send(StopRequested(reason="stop_requested"))
            await self._maybe_finish_stop()
        elif state in (CaptureState.STOPPED, CaptureState.FAILED):
            await self.send(SessionStopped(capture_session_id=self.auth.capture_session_id))
            self.closing = True
            try:
                await self.ws.close(code=1000)
            except _SEND_ERRORS:
                pass


def _capture_state(session: Session, capture_session_id: str) -> str | None:
    cs = session.get(CaptureSession, capture_session_id)
    return cs.state if cs else None


def _publish_source_closed(session: Session, meeting_id: str, source_key: str, reason: str) -> None:
    publish(session, meeting_id, "source.state", {"source_id": source_key, "state": "closed", "reason": reason})


class _suppress_cancel:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is asyncio.CancelledError


async def ingest_endpoint(websocket: WebSocket) -> None:
    settings = get_settings()
    origin = websocket.headers.get("origin")
    kind = ingest_origin_kind(origin, settings)
    if kind is None:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    await IngestConnection(websocket, settings, kind).run()


async def stop_supervisor(stop_event: asyncio.Event, interval_s: float = 2.0) -> None:
    """Finish STOPPING sessions that have no live connection in this process (grace timeout)."""
    settings = get_settings()

    def _tick(session: Session) -> list[str]:
        from sqlalchemy import select

        ids = session.scalars(select(CaptureSession.id).where(CaptureSession.state == CaptureState.STOPPING)).all()
        done = []
        for cs_id in ids:
            if registry.get(cs_id) is None and service.finalize_stop_if_ready(session, cs_id, settings.stop_grace_s,
                                                                              force=False):
                done.append(cs_id)
        return done

    while not stop_event.is_set():
        try:
            await run_db(_tick)
        except Exception:  # noqa: BLE001 - supervisor must survive transient DB errors; logged
            log.exception("stop supervisor tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            pass
