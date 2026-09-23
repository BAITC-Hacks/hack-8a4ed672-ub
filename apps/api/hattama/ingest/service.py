"""Database side of audio ingestion (synchronous; called from a thread by the WebSocket handler)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from hattama_contracts.messages import Hello, SourceOpen
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from hattama.db.models import (
    AudioGap,
    AuthSession,
    CaptureSession,
    CaptureSource,
    IngestToken,
    Meeting,
    MeetingMember,
    SourceEpoch,
)
from hattama.db.types import utcnow
from hattama.domain import meeting_status
from hattama.domain.enums import CaptureMode, CaptureState, MeetingStatus, MemberRole, SourceKind
from hattama.jobs.queue import enqueue
from hattama.security.tokens import hash_token
from hattama.services.events import audit, publish

ALLOWED_KINDS: dict[str, set[str]] = {
    CaptureMode.COMPANION: {SourceKind.TAB_AUDIO, SourceKind.MICROPHONE},
    CaptureMode.BROWSER_TAB: {SourceKind.TAB_AUDIO, SourceKind.MICROPHONE},
    CaptureMode.LOCAL_MIC: {SourceKind.LOCAL_MICROPHONE},
    CaptureMode.BOT: {SourceKind.BOT_AUDIO},
}


class IngestError(Exception):
    def __init__(self, code: str, message: str, fatal: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.fatal = fatal


@dataclass
class IngestAuth:
    capture_session_id: str
    meeting_id: str
    user_id: str
    actor_kind: str  # extension | web | agent
    mode: str


def authenticate_hello(session: Session, hello: Hello, origin_kind: str, cookie_token: str | None) -> IngestAuth:
    now = utcnow()
    if hello.token:
        tok = session.scalar(select(IngestToken).where(IngestToken.token_hash == hash_token(hello.token)))
        if tok is None or tok.revoked_at is not None or tok.expires_at < now:
            raise IngestError("unauthorized", "Токен недействителен или истёк. Выполните сопряжение заново.")
        cs = session.get(CaptureSession, tok.capture_session_id)
        user_id = tok.user_id
        if tok.scope == "agent" and origin_kind != "agent":
            raise IngestError("forbidden", "Токен бота нельзя использовать из браузера")
        actor = "agent" if tok.scope == "agent" else ("extension" if origin_kind == "extension" else origin_kind)
    else:
        if origin_kind != "web" or not cookie_token or not hello.capture_session_id:
            raise IngestError("unauthorized", "Требуется токен сопряжения или сессия веб-интерфейса")
        auth = session.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(cookie_token)))
        if auth is None or auth.revoked_at is not None or auth.expires_at < now:
            raise IngestError("unauthorized", "Сессия истекла")
        cs = session.get(CaptureSession, hello.capture_session_id)
        user_id = auth.user_id
        actor = "web"
        if cs is not None:
            member = session.scalar(select(MeetingMember).where(MeetingMember.meeting_id == cs.meeting_id,
                                                                MeetingMember.user_id == user_id))
            if member is None or member.role != MemberRole.SECRETARY:
                raise IngestError("forbidden", "Нет прав секретаря на эту встречу")
    if cs is None:
        raise IngestError("not_found", "Сессия захвата не найдена")
    if hello.capture_session_id and hello.capture_session_id != cs.id:
        raise IngestError("forbidden", "Токен выдан для другой сессии захвата")
    if cs.state not in (CaptureState.CREATED, CaptureState.ACTIVE, CaptureState.STOPPING):
        raise IngestError("session_closed", f"Сессия захвата в состоянии {cs.state}; запись остановлена")
    meeting = session.get(Meeting, cs.meeting_id)
    if meeting is None or meeting.deleted_at is not None:
        raise IngestError("not_found", "Встреча не найдена")
    if meeting.consent_confirmed_at is None:
        raise IngestError("consent_required", "Не подтверждено уведомление участников о записи")
    if cs.mode == CaptureMode.UPLOAD:
        raise IngestError("forbidden", "Сессия загрузки файла не принимает поток")
    if actor == "web" and cs.mode not in (CaptureMode.LOCAL_MIC, CaptureMode.BROWSER_TAB):
        raise IngestError("forbidden", "Из веб-интерфейса доступны запись микрофона и вкладки")
    if cs.mode == CaptureMode.BOT and actor != "agent":
        raise IngestError("forbidden", "Сессия бота принимает поток только от meeting-agent")
    return IngestAuth(cs.id, cs.meeting_id, user_id, actor, cs.mode)


def mark_connected(session: Session, auth: IngestAuth, client: dict) -> None:
    cs = session.get(CaptureSession, auth.capture_session_id)
    assert cs is not None
    first = cs.state == CaptureState.CREATED
    if first:
        cs.state = CaptureState.ACTIVE
        cs.started_at = utcnow()
    cs.connected = True
    cs.client_info = client
    meeting_status.transition(session, auth.meeting_id, MeetingStatus.LIVE)
    publish(session, auth.meeting_id, "capture.state",
            {"capture_session_id": cs.id, "state": cs.state, "connected": True, "client": client})
    audit(session, action="capture.connected" if not first else "capture.started", actor_user_id=auth.user_id,
          actor_kind=auth.actor_kind, meeting_id=auth.meeting_id, object_type="capture_session", object_id=cs.id,
          after={"client": client})


def mark_disconnected(session: Session, auth: IngestAuth, reason: str) -> None:
    session.execute(update(CaptureSession).where(CaptureSession.id == auth.capture_session_id)
                    .values(connected=False))
    publish(session, auth.meeting_id, "capture.state",
            {"capture_session_id": auth.capture_session_id, "connected": False, "reason": reason})


@dataclass
class OpenedSource:
    source_db_id: str
    source_index: int
    epoch_id: str
    rel_path: str
    durable_sequence: int
    durable_samples: int
    sample_rate: int
    channel_count: int
    epoch_start_wall_us: int
    source_key: str
    kind: str
    closed: bool = False


def open_source(session: Session, auth: IngestAuth, msg: SourceOpen, max_sources: int) -> OpenedSource:
    if msg.kind not in ALLOWED_KINDS.get(auth.mode, set()):
        raise IngestError("bad_source_kind", f"Источник {msg.kind} недопустим для режима {auth.mode}", fatal=False)
    now_us = int(utcnow().timestamp() * 1_000_000)
    if msg.epoch_start_wall_us > now_us + 5_000_000 or msg.epoch_start_wall_us < now_us - 24 * 3600 * 1_000_000:
        raise IngestError("bad_clock", "Время начала эпохи расходится с часами сервера более допустимого", fatal=False)
    src = session.scalar(select(CaptureSource).where(CaptureSource.capture_session_id == auth.capture_session_id,
                                                     CaptureSource.source_key == msg.source_id))
    cs = session.get(CaptureSession, auth.capture_session_id)
    assert cs is not None
    if src is None:
        if cs.state == CaptureState.STOPPING:
            raise IngestError("session_stopping", "Запись завершается; новый источник открыть нельзя", fatal=False)
        count = session.scalar(select(func.count()).select_from(CaptureSource)
                               .where(CaptureSource.capture_session_id == auth.capture_session_id)) or 0
        if count >= max_sources:
            raise IngestError("too_many_sources", "Превышено число источников", fatal=False)
        src = CaptureSource(capture_session_id=auth.capture_session_id, source_key=msg.source_id,
                            source_index=count, kind=msg.kind, label=msg.label)
        session.add(src)
        session.flush()
    elif src.kind != msg.kind:
        raise IngestError("source_kind_changed", "Тип источника не может меняться", fatal=False)
    epoch = session.scalar(select(SourceEpoch).where(SourceEpoch.source_id == src.id,
                                                     SourceEpoch.epoch == msg.capture_epoch))
    if epoch is not None:
        if epoch.sample_rate != msg.sample_rate or epoch.channel_count != msg.channel_count:
            raise IngestError("epoch_format_changed", "Формат эпохи не может меняться", fatal=False)
    else:
        if cs.state == CaptureState.STOPPING:
            raise IngestError("session_stopping", "Запись завершается; новую эпоху открыть нельзя", fatal=False)
        for old in session.scalars(select(SourceEpoch).where(SourceEpoch.source_id == src.id,
                                                             SourceEpoch.closed_at.is_(None))):
            old.closed_at = utcnow()
            old.close_reason = "superseded"
        rel = f"captures/{auth.capture_session_id}/{msg.source_id}/epoch-{msg.capture_epoch}.pcm"
        epoch = SourceEpoch(source_id=src.id, epoch=msg.capture_epoch, sample_rate=msg.sample_rate,
                            channel_count=msg.channel_count, epoch_start_wall_us=msg.epoch_start_wall_us,
                            rel_path=rel)
        session.add(epoch)
        cs = session.get(CaptureSession, auth.capture_session_id)
        assert cs is not None
        if cs.timeline_origin_us is None or msg.epoch_start_wall_us < cs.timeline_origin_us:
            if cs.timeline_origin_us is None:
                cs.timeline_origin_us = msg.epoch_start_wall_us
        session.flush()
    if epoch.closed_at is None:
        src.last_state = "open"
        publish(session, auth.meeting_id, "source.state",
                {"capture_session_id": auth.capture_session_id, "source_id": msg.source_id, "kind": msg.kind,
                 "state": "open", "epoch": msg.capture_epoch, "sample_rate": msg.sample_rate, "label": msg.label})
    return OpenedSource(src.id, src.source_index, epoch.id, epoch.rel_path, epoch.durable_sequence,
                        epoch.durable_samples, epoch.sample_rate, epoch.channel_count, epoch.epoch_start_wall_us,
                        src.source_key, src.kind, epoch.closed_at is not None)


def persist_progress(session: Session, epoch_id: str, durable_sequence: int, durable_samples: int,
                     received: int, duplicates: int, capture_session_id: str) -> None:
    # A superseded socket may finish cleanup after a reconnect has progressed. Never move the
    # durable checkpoint backwards, or the next client would replay already acknowledged audio.
    session.execute(update(SourceEpoch).where(SourceEpoch.id == epoch_id,
                                               SourceEpoch.durable_sequence <= durable_sequence).values(
        durable_sequence=durable_sequence, durable_samples=durable_samples,
        received_frames=case((SourceEpoch.received_frames < received, received), else_=SourceEpoch.received_frames),
        duplicate_frames=case((SourceEpoch.duplicate_frames < duplicates, duplicates),
                              else_=SourceEpoch.duplicate_frames)))
    session.execute(update(CaptureSession).where(CaptureSession.id == capture_session_id)
                    .values(last_frame_at=utcnow()))


def record_gap(session: Session, meeting_id: str, epoch_id: str, source_key: str, start: int, end: int,
               reason: str, sample_rate: int) -> None:
    session.add(AudioGap(source_epoch_id=epoch_id, start_sample=start, end_sample=end, reason=reason))
    publish(session, meeting_id, "source.gap", {"source_id": source_key, "start_sample": start, "end_sample": end,
                                                "seconds": round((end - start) / sample_rate, 3), "reason": reason})


def narrow_gap(session: Session, epoch_id: str, start: int, end: int) -> None:
    """A late frame arrived inside a declared gap: shrink/resolve the gap when it touches a boundary."""
    for gap in session.scalars(select(AudioGap).where(AudioGap.source_epoch_id == epoch_id,
                                                      AudioGap.resolved.is_(False))):
        if start <= gap.start_sample and end >= gap.end_sample:
            gap.resolved = True
        elif start <= gap.start_sample < end:
            gap.start_sample = end
        elif start < gap.end_sample <= end:
            gap.end_sample = start


def close_epoch(session: Session, epoch_id: str, final_sequence: int | None, reason: str) -> None:
    epoch = session.get(SourceEpoch, epoch_id)
    if epoch is None or epoch.closed_at is not None:
        return
    epoch.closed_at = utcnow()
    epoch.close_reason = reason
    epoch.final_sequence = final_sequence


def set_source_state(session: Session, auth: IngestAuth, source_index: int, state: str) -> str | None:
    src = session.scalar(select(CaptureSource).where(CaptureSource.capture_session_id == auth.capture_session_id,
                                                     CaptureSource.source_index == source_index))
    if src is None:
        return None
    src.last_state = state
    if state in ("muted", "unmuted"):
        src.muted = state == "muted"
        audit(session, action=f"source.{state}", actor_user_id=auth.user_id, actor_kind=auth.actor_kind,
              meeting_id=auth.meeting_id, object_type="capture_source", object_id=src.id)
    publish(session, auth.meeting_id, "source.state",
            {"capture_session_id": auth.capture_session_id, "source_id": src.source_key, "kind": src.kind,
             "state": state, "muted": src.muted})
    return src.source_key


def request_stop(session: Session, capture_session_id: str, reason: str, actor_user_id: str | None,
                 actor_kind: str) -> bool:
    res = session.execute(
        update(CaptureSession)
        .where(CaptureSession.id == capture_session_id,
               CaptureSession.state.in_([CaptureState.ACTIVE, CaptureState.CREATED]))
        .values(state=CaptureState.STOPPING, stop_requested_at=utcnow(), stop_reason=reason)
    )
    changed = bool(res.rowcount)  # type: ignore[attr-defined]
    cs = session.get(CaptureSession, capture_session_id)
    if changed and cs is not None:
        publish(session, cs.meeting_id, "capture.state",
                {"capture_session_id": cs.id, "state": CaptureState.STOPPING, "reason": reason})
        audit(session, action="capture.stop_requested", actor_user_id=actor_user_id, actor_kind=actor_kind,
              meeting_id=cs.meeting_id, object_type="capture_session", object_id=cs.id, after={"reason": reason})
    return changed


def open_epoch_ids(session: Session, capture_session_id: str) -> list[str]:
    return list(session.scalars(
        select(SourceEpoch.id).join(CaptureSource, CaptureSource.id == SourceEpoch.source_id)
        .where(CaptureSource.capture_session_id == capture_session_id, SourceEpoch.closed_at.is_(None))
    ))


def finalize_stop_if_ready(session: Session, capture_session_id: str, grace_s: float, force: bool = False) -> bool:
    """STOPPING -> STOPPED once all epochs are closed (or grace expired); enqueue finalization exactly once."""
    cs = session.get(CaptureSession, capture_session_id)
    if cs is None or cs.state != CaptureState.STOPPING:
        return False
    open_ids = open_epoch_ids(session, capture_session_id)
    expired = cs.stop_requested_at is not None and utcnow() - cs.stop_requested_at > timedelta(seconds=grace_s)
    if open_ids and not (force or expired):
        return False
    for epoch_id in open_ids:
        close_epoch(session, epoch_id, None, "stop_timeout")
    res = session.execute(
        update(CaptureSession)
        .where(CaptureSession.id == capture_session_id, CaptureSession.state == CaptureState.STOPPING)
        .values(state=CaptureState.STOPPED, stopped_at=utcnow(), connected=False)
    )
    if not res.rowcount:  # type: ignore[attr-defined]
        return False  # another process finished it
    recorded_samples = session.scalar(select(func.sum(SourceEpoch.durable_samples)).join(
        CaptureSource, CaptureSource.id == SourceEpoch.source_id).where(
            CaptureSource.capture_session_id == capture_session_id)) or 0
    target = MeetingStatus.PROCESSING if recorded_samples else MeetingStatus.READY
    meeting_status.transition(session, cs.meeting_id, target)
    if recorded_samples:
        enqueue(session, kind="finalize_meeting", key=f"finalize:{cs.meeting_id}:{capture_session_id}",
                meeting_id=cs.meeting_id, payload={"capture_session_id": capture_session_id}, priority=50)
    publish(session, cs.meeting_id, "capture.state",
            {"capture_session_id": cs.id, "state": CaptureState.STOPPED, "open_epochs_force_closed": len(open_ids)})
    publish(session, cs.meeting_id, "meeting.status", {"status": target})
    audit(session, action="capture.stopped", actor_user_id=None, actor_kind="system", meeting_id=cs.meeting_id,
          object_type="capture_session", object_id=cs.id, after={"force_closed_epochs": len(open_ids)})
    return True
