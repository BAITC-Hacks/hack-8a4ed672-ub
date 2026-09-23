from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from hattama.api.deps import client_ip, current_user, get_db, meeting_for, rate_limiter
from hattama.config import Settings, get_settings
from hattama.db.models import (
    AudioGap,
    CaptureSession,
    CaptureSource,
    IngestToken,
    Meeting,
    PairingCode,
    SourceEpoch,
    User,
)
from hattama.db.types import utcnow
from hattama.domain.enums import CaptureMode, CaptureState, MeetingStatus
from hattama.ingest.service import finalize_stop_if_ready, request_stop
from hattama.security.origins import allowed_extension_origin
from hattama.security.tokens import hash_token, new_pairing_code, new_token, normalize_pairing_code
from hattama.services.events import audit, publish

router = APIRouter(prefix="/api/v1", tags=["capture"])
MAX_PAIRING_ATTEMPTS = 5


class CaptureCreateIn(BaseModel):
    mode: Literal["companion", "local_mic", "browser_tab"]


class PairingOut(BaseModel):
    code: str
    expires_at: datetime


class GapOut(BaseModel):
    start_s: float
    end_s: float
    reason: str
    resolved: bool


class EpochOut(BaseModel):
    epoch: int
    sample_rate: int
    channel_count: int
    durable_seconds: float
    received_frames: int
    duplicate_frames: int
    closed: bool
    close_reason: str | None
    gaps: list[GapOut]


class SourceOut(BaseModel):
    source_id: str
    kind: str
    label: str
    state: str | None
    muted: bool
    epochs: list[EpochOut]


class CaptureOut(BaseModel):
    id: str
    meeting_id: str
    mode: str
    state: str
    connected: bool
    created_at: datetime
    started_at: datetime | None
    stopped_at: datetime | None
    stop_reason: str | None
    last_frame_at: datetime | None
    client_info: dict
    sources: list[SourceOut]
    pairing: PairingOut | None = None


def capture_out(db: Session, cs: CaptureSession, pairing: PairingOut | None = None) -> CaptureOut:
    sources = []
    for src in db.scalars(select(CaptureSource).where(CaptureSource.capture_session_id == cs.id)
                          .order_by(CaptureSource.source_index)):
        epochs = []
        for ep in db.scalars(select(SourceEpoch).where(SourceEpoch.source_id == src.id).order_by(SourceEpoch.epoch)):
            gaps = [GapOut(start_s=g.start_sample / ep.sample_rate, end_s=g.end_sample / ep.sample_rate,
                           reason=g.reason, resolved=g.resolved)
                    for g in db.scalars(select(AudioGap).where(AudioGap.source_epoch_id == ep.id))]
            epochs.append(EpochOut(epoch=ep.epoch, sample_rate=ep.sample_rate, channel_count=ep.channel_count,
                                   durable_seconds=round(ep.durable_samples / ep.sample_rate, 2),
                                   received_frames=ep.received_frames, duplicate_frames=ep.duplicate_frames,
                                   closed=ep.closed_at is not None, close_reason=ep.close_reason, gaps=gaps))
        sources.append(SourceOut(source_id=src.source_key, kind=src.kind, label=src.label, state=src.last_state,
                                 muted=src.muted, epochs=epochs))
    return CaptureOut(id=cs.id, meeting_id=cs.meeting_id, mode=cs.mode, state=cs.state, connected=cs.connected,
                      created_at=cs.created_at, started_at=cs.started_at, stopped_at=cs.stopped_at,
                      stop_reason=cs.stop_reason, last_frame_at=cs.last_frame_at, client_info=cs.client_info or {},
                      sources=sources, pairing=pairing)


def _new_pairing(db: Session, cs: CaptureSession, user: User, settings: Settings) -> PairingOut:
    code = new_pairing_code()
    expires = utcnow() + timedelta(seconds=settings.pairing_code_ttl_s)
    db.add(PairingCode(code_hash=hash_token(code), capture_session_id=cs.id, user_id=user.id, expires_at=expires))
    return PairingOut(code=code, expires_at=expires)


@router.post("/meetings/{meeting_id}/capture-sessions", response_model=CaptureOut, status_code=201)
def create_capture(meeting_id: str, body: CaptureCreateIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> CaptureOut:
    meeting = meeting_for(db, user, meeting_id, "capture")
    if meeting.consent_confirmed_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала подтвердите уведомление участников о записи")
    if meeting.status not in (MeetingStatus.READY, MeetingStatus.LIVE):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Запись недоступна в статусе {meeting.status}")
    active = db.scalar(select(CaptureSession).where(
        CaptureSession.meeting_id == meeting_id,
        CaptureSession.state.in_([CaptureState.CREATED, CaptureState.ACTIVE, CaptureState.STOPPING])))
    if active is not None:
        if active.mode != body.mode:
            raise HTTPException(status.HTTP_409_CONFLICT, "Уже есть активная сессия захвата другого типа")
        pairing = _new_pairing(db, active, user, settings) if active.mode == CaptureMode.COMPANION else None
        return capture_out(db, active, pairing)
    cs = CaptureSession(meeting_id=meeting_id, mode=body.mode, created_by=user.id)
    db.add(cs)
    db.flush()
    pairing = _new_pairing(db, cs, user, settings) if body.mode == CaptureMode.COMPANION else None
    audit(db, action="capture.created", actor_user_id=user.id, meeting_id=meeting_id,
          object_type="capture_session", object_id=cs.id, after={"mode": body.mode})
    return capture_out(db, cs, pairing)


@router.get("/meetings/{meeting_id}/capture-sessions", response_model=list[CaptureOut])
def list_captures(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[CaptureOut]:
    meeting_for(db, user, meeting_id, "read")
    rows = db.scalars(select(CaptureSession).where(CaptureSession.meeting_id == meeting_id)
                      .order_by(CaptureSession.created_at))
    return [capture_out(db, cs) for cs in rows]


def _capture_for(db: Session, user: User, capture_session_id: str) -> CaptureSession:
    cs = db.get(CaptureSession, capture_session_id)
    if cs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия захвата не найдена")
    meeting_for(db, user, cs.meeting_id, "capture")
    return cs


@router.post("/capture-sessions/{capture_session_id}/pairing-code", response_model=PairingOut)
def new_pairing_code_route(capture_session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
                           settings: Settings = Depends(get_settings)) -> PairingOut:
    cs = _capture_for(db, user, capture_session_id)
    if cs.mode != CaptureMode.COMPANION or cs.state not in (CaptureState.CREATED, CaptureState.ACTIVE):
        raise HTTPException(status.HTTP_409_CONFLICT, "Сопряжение недоступно для этой сессии")
    return _new_pairing(db, cs, user, settings)


@router.post("/capture-sessions/{capture_session_id}/stop", response_model=CaptureOut)
def stop_capture(capture_session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> CaptureOut:
    cs = _capture_for(db, user, capture_session_id)
    request_stop(db, cs.id, "user_stop", user.id, "user")
    # A cancelled permission dialog may leave a session with no open sources. Finish it immediately.
    if not cs.connected:
        finalize_stop_if_ready(db, cs.id, settings.stop_grace_s)
    db.flush()
    db.refresh(cs)
    return capture_out(db, cs)


@router.post("/capture-sessions/{capture_session_id}/revoke-tokens", status_code=204)
def revoke_tokens(capture_session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    cs = _capture_for(db, user, capture_session_id)
    for tok in db.scalars(select(IngestToken).where(IngestToken.capture_session_id == cs.id,
                                                    IngestToken.revoked_at.is_(None))):
        tok.revoked_at = utcnow()
    audit(db, action="capture.tokens_revoked", actor_user_id=user.id, meeting_id=cs.meeting_id,
          object_type="capture_session", object_id=cs.id)


# ---------------------------------------------------------------- extension pairing (no cookie)
class PairingExchangeIn(BaseModel):
    code: str = Field(min_length=8, max_length=16)
    client_name: str = Field(default="hattama-companion", max_length=64)
    client_version: str = Field(default="", max_length=32)


class PairingExchangeOut(BaseModel):
    token: str
    expires_at: datetime
    capture_session_id: str
    meeting_id: str
    meeting_title: str
    ws_path: str = "/api/v1/ingest/ws"
    protocol: str = "hattama.audio.v1"


@router.post("/pairing/exchange", response_model=PairingExchangeOut)
def pairing_exchange(body: PairingExchangeIn, request: Request, db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> PairingExchangeOut:
    origin = request.headers.get("origin")
    if not allowed_extension_origin(origin, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Расширение не разрешено администратором (HATTAMA_ALLOWED_EXTENSION_IDS)")
    rate_limiter.check(f"pairing:{client_ip(request)}", 10)
    code = normalize_pairing_code(body.code)
    record = db.scalar(select(PairingCode).where(PairingCode.code_hash == hash_token(code))) if code else None
    now = utcnow()
    if record is None or record.used_at is not None or record.expires_at < now \
            or record.attempts >= MAX_PAIRING_ATTEMPTS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Код сопряжения недействителен или истёк")
    cs = db.get(CaptureSession, record.capture_session_id)
    if cs is None or cs.state not in (CaptureState.CREATED, CaptureState.ACTIVE):
        raise HTTPException(status.HTTP_409_CONFLICT, "Сессия захвата уже завершена")
    meeting = db.get(Meeting, cs.meeting_id)
    assert meeting is not None
    record.used_at = now
    token = new_token()
    expires = now + timedelta(seconds=settings.ingest_token_ttl_s)
    db.add(IngestToken(token_hash=hash_token(token), capture_session_id=cs.id, user_id=record.user_id,
                       scope="ingest", client_name=body.client_name, expires_at=expires))
    audit(db, action="capture.paired", actor_user_id=record.user_id, actor_kind="extension",
          meeting_id=cs.meeting_id, object_type="capture_session", object_id=cs.id,
          after={"client": body.client_name, "version": body.client_version})
    publish(db, cs.meeting_id, "capture.state", {"capture_session_id": cs.id, "paired": True})
    return PairingExchangeOut(token=token, expires_at=expires, capture_session_id=cs.id, meeting_id=meeting.id,
                              meeting_title=meeting.title)
