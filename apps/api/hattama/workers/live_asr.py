"""Live ASR loop: reads durable audio of active capture epochs and publishes partial/stable segments.

Runs inside the ASR worker process, which owns the single loaded ASR model (economy profile: one copy
in VRAM). Live work always has priority over final-pass slices (see workers/asr_worker.py).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hattama.asr.base import ASRProvider
from hattama.audio.reader import EpochReader
from hattama.audio.vad import Vad
from hattama.config import Settings
from hattama.db.models import (
    CaptureSession,
    CaptureSource,
    Meeting,
    Participant,
    SourceEpoch,
    TranscriptRevision,
    TranscriptSegment,
)
from hattama.db.session import session_scope
from hattama.domain.enums import CaptureMode, CaptureState, RevisionKind, SegmentState, SourceKind
from hattama.jobs.queue import enqueue
from hattama.live.transcriber import LiveConfig, LiveOutput, LiveTranscriber
from hattama.runtime import participant_glossary
from hattama.services.events import metric, publish

log = logging.getLogger(__name__)
MAX_READ_S = 10.0
METRICS_EVERY_S = 2.0
LIVE_EXTRACT_EVERY_SEGMENTS = 6


@dataclass
class LiveStream:
    epoch_id: str
    meeting_id: str
    capture_session_id: str
    source_key: str
    source_kind: str
    sample_rate: int
    offset_ms: float  # epoch start on the capture-session timeline
    epoch_start_wall_us: int
    reader: EpochReader
    transcriber: LiveTranscriber
    last_metrics: float = 0.0
    stable_since_extract: int = 0
    last_step_rtf: float | None = None
    last_checkpoint: float = field(default_factory=time.monotonic)


def live_revision_id(session: Session, meeting_id: str) -> str:
    rev = session.scalar(select(TranscriptRevision).where(TranscriptRevision.meeting_id == meeting_id,
                                                          TranscriptRevision.kind == RevisionKind.LIVE))
    if rev is not None:
        return rev.id
    rev = TranscriptRevision(meeting_id=meeting_id, number=0, kind=RevisionKind.LIVE, status="building")
    try:
        with session.begin_nested():
            session.add(rev)
    except IntegrityError:
        existing = session.scalar(select(TranscriptRevision.id).where(TranscriptRevision.meeting_id == meeting_id,
                                                                      TranscriptRevision.number == 0))
        assert existing is not None
        return existing
    return rev.id


class LiveAsrLoop:
    def __init__(self, settings: Settings, asr: ASRProvider, vad: Vad, live_cfg: dict, analysis_enabled: bool) -> None:
        self.settings = settings
        self.asr = asr
        self.vad = vad
        self.live_cfg = live_cfg
        self.analysis_enabled = analysis_enabled
        self.streams: dict[str, LiveStream] = {}

    # ------------------------------------------------------------------ discovery
    def _active(self, session: Session) -> list[tuple[SourceEpoch, CaptureSource, CaptureSession]]:
        rows = session.execute(
            select(SourceEpoch, CaptureSource, CaptureSession)
            .join(CaptureSource, CaptureSource.id == SourceEpoch.source_id)
            .join(CaptureSession, CaptureSession.id == CaptureSource.capture_session_id)
            .where(SourceEpoch.live_done.is_(False),
                   CaptureSession.mode != CaptureMode.UPLOAD,
                   CaptureSession.state.in_([CaptureState.ACTIVE, CaptureState.STOPPING, CaptureState.STOPPED]))
        ).all()
        return [(e, s, c) for e, s, c in rows]

    def _open_stream(self, session: Session, epoch: SourceEpoch, src: CaptureSource, cs: CaptureSession) -> LiveStream:
        meeting = session.get(Meeting, cs.meeting_id)
        assert meeting is not None
        names = [p.display_name for p in session.scalars(select(Participant).where(Participant.meeting_id == meeting.id))]
        cfg = LiveConfig(step_s=float(self.live_cfg.get("step_s", 1.5)),
                         max_window_s=float(self.live_cfg.get("max_window_s", 20.0)),
                         language_mode=meeting.language_mode, beam_size=int(self.live_cfg.get("beam_size", 1)))
        resume_native = epoch.live_processed_samples
        reader = EpochReader(self.settings.data_dir / epoch.rel_path, epoch.sample_rate, epoch.channel_count,
                             start_native=resume_native)
        transcriber = LiveTranscriber(self.asr, self.vad, cfg, glossary=participant_glossary(names),
                                      start_s=resume_native / epoch.sample_rate)
        origin = cs.timeline_origin_us or epoch.epoch_start_wall_us
        stream = LiveStream(epoch.id, cs.meeting_id, cs.id, src.source_key, src.kind, epoch.sample_rate,
                            (epoch.epoch_start_wall_us - origin) / 1000.0, epoch.epoch_start_wall_us, reader,
                            transcriber)
        log.info("live stream opened", extra={"epoch": epoch.id, "source": src.source_key,
                                              "resume_s": round(resume_native / epoch.sample_rate, 2)})
        return stream

    # ------------------------------------------------------------------ main tick
    def tick(self) -> bool:
        """Process every active stream once. Returns True if any recognition work was done."""
        worked = False
        with session_scope() as session:
            active = self._active(session)
            snapshot = [(e.id, e.durable_samples, e.closed_at is not None, e, s, c) for e, s, c in active]
            for epoch_id, _, _, epoch, src, cs in snapshot:
                if epoch_id not in self.streams:
                    self.streams[epoch_id] = self._open_stream(session, epoch, src, cs)
        active_ids = {row[0] for row in snapshot}
        for epoch_id in list(self.streams):
            if epoch_id not in active_ids:
                del self.streams[epoch_id]
        for epoch_id, durable, closed, *_ in snapshot:
            stream = self.streams[epoch_id]
            audio = stream.reader.read_available(durable, max_native=int(MAX_READ_S * stream.sample_rate))
            stream.transcriber.push(audio)
            caught_up = stream.reader.read_pos >= durable
            outputs: list[LiveOutput] = []
            done = False
            if closed and caught_up:
                stream.reader.read_available(durable, final=True)
                outputs = stream.transcriber.finish()
                done = True
                worked = True
            elif stream.transcriber.ready():
                outputs = stream.transcriber.step()
                worked = True
                st = stream.transcriber.last_stats
                if st is not None:
                    stream.last_step_rtf = st.processing_s / max(stream.transcriber.cfg.step_s, 1e-6)
            if outputs or done or time.monotonic() - stream.last_metrics >= METRICS_EVERY_S:
                self._write(stream, outputs, durable, done)
            if done:
                del self.streams[epoch_id]
        return worked

    # ------------------------------------------------------------------ persistence
    def _write(self, stream: LiveStream, outputs: list[LiveOutput], durable: int, done: bool) -> None:
        now_wall = time.time()
        with session_scope() as session:
            rev_id = live_revision_id(session, stream.meeting_id) if outputs else None
            for out in outputs:
                start_ms = int(round(stream.offset_ms + out.start_s * 1000))
                end_ms = int(round(stream.offset_ms + out.end_s * 1000))
                audio_end_wall = stream.epoch_start_wall_us / 1e6 + out.end_s
                latency = max(0.0, now_wall - audio_end_wall)
                payload = {"segment_id": out.utterance_id, "revision": out.revision, "source_id": stream.source_key,
                           "source_kind": stream.source_kind, "start_ms": start_ms, "end_ms": end_ms,
                           "text": out.text, "language": out.language, "review_reasons": out.review_reasons,
                           "latency_s": round(latency, 2)}
                if out.kind == "partial":
                    publish(session, stream.meeting_id, "segment.partial", payload)
                    metric(session, "partial_latency_s", latency, stream.meeting_id, source=stream.source_key)
                    continue
                assert rev_id is not None
                seg = TranscriptSegment(
                    id=out.utterance_id, meeting_id=stream.meeting_id, revision_id=rev_id,
                    capture_session_id=stream.capture_session_id, source_key=stream.source_key, start_ms=start_ms,
                    end_ms=end_ms, text=out.text, original_text=out.text, language=out.language,
                    words=[{"w": w.text, "s": round(stream.offset_ms / 1000 + w.start, 3),
                            "e": round(stream.offset_ms / 1000 + w.end, 3), "p": round(w.probability, 3)}
                           for w in out.words],
                    state=SegmentState.STABLE, review_reasons=out.review_reasons, avg_logprob=out.avg_probability,
                )
                session.add(seg)
                publish(session, stream.meeting_id, "segment.stable", {**payload, "speaker": _live_speaker(stream)})
                metric(session, "stable_latency_s", latency, stream.meeting_id, source=stream.source_key)
                stream.stable_since_extract += 1
            backlog = stream.reader.backlog_seconds(durable) + stream.transcriber.pending_seconds()
            publish(session, stream.meeting_id, "asr.metrics",
                    {"source_id": stream.source_key, "backlog_s": round(backlog, 2),
                     "step_rtf": round(stream.last_step_rtf, 3) if stream.last_step_rtf is not None else None,
                     "processed_s": round(stream.reader.read_pos / stream.sample_rate, 2),
                     "durable_s": round(durable / stream.sample_rate, 2), "done": done})
            metric(session, "asr_backlog_s", backlog, stream.meeting_id, source=stream.source_key)
            if stream.last_step_rtf is not None:
                metric(session, "asr_rtf", stream.last_step_rtf, stream.meeting_id, source=stream.source_key)
            stream.last_metrics = time.monotonic()
            checkpoint = int(stream.transcriber.committed_end * stream.sample_rate)
            session.execute(update(SourceEpoch).where(SourceEpoch.id == stream.epoch_id)
                            .values(live_processed_samples=checkpoint, live_done=done))
            if self.analysis_enabled and (stream.stable_since_extract >= LIVE_EXTRACT_EVERY_SEGMENTS
                                          or (done and stream.stable_since_extract)):
                bucket = int(time.time() // 30)
                enqueue(session, kind="live_extract", key=f"live_extract:{stream.meeting_id}:{bucket}",
                        meeting_id=stream.meeting_id, payload={}, priority=200, max_attempts=1)
                stream.stable_since_extract = 0


def _live_speaker(stream: LiveStream) -> dict:
    if stream.source_kind == SourceKind.MICROPHONE or stream.source_kind == SourceKind.LOCAL_MICROPHONE:
        return {"label": "mic", "display": "Микрофон", "provisional": True}
    return {"label": "remote", "display": "Звук встречи (говорящий не определён)", "provisional": True}


def pcm_path(settings: Settings, rel: str) -> Path:
    return settings.data_dir / rel
