"""Dashboard live channel: authenticated WebSocket streaming DB-backed LiveEvents, plus a REST snapshot."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anyio
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hattama.api.deps import SESSION_COOKIE, current_user, get_db, meeting_for, member_role
from hattama.config import get_settings
from hattama.db.models import (
    ActionItem,
    AuthSession,
    CaptureSession,
    Job,
    LiveEvent,
    Meeting,
    MetricSample,
    TranscriptRevision,
    TranscriptSegment,
    User,
)
from hattama.db.session import session_scope
from hattama.db.types import utcnow
from hattama.security.tokens import hash_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["live"])
POLL_INTERVAL_S = 0.3
BATCH = 200


def _authorize_ws(session: Session, cookie: str | None, meeting_id: str) -> str | None:
    if not cookie:
        return None
    auth = session.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(cookie)))
    if auth is None or auth.revoked_at is not None or auth.expires_at < utcnow():
        return None
    meeting = session.get(Meeting, meeting_id)
    if meeting is None or meeting.deleted_at is not None:
        return None
    return auth.user_id if member_role(session, meeting_id, auth.user_id) else None


def _fetch(session: Session, meeting_id: str, after: int) -> list[dict[str, Any]]:
    rows = session.scalars(select(LiveEvent).where(LiveEvent.meeting_id == meeting_id, LiveEvent.id > after)
                           .order_by(LiveEvent.id).limit(BATCH)).all()
    return [{"id": r.id, "type": r.type, "meeting_id": r.meeting_id, "at": r.created_at.isoformat(),
             "payload": r.payload} for r in rows]


def _last_id(session: Session, meeting_id: str) -> int:
    return session.scalar(select(func.max(LiveEvent.id)).where(LiveEvent.meeting_id == meeting_id)) or 0


@router.websocket("/meetings/{meeting_id}/events/ws")
async def events_ws(websocket: WebSocket, meeting_id: str) -> None:
    settings = get_settings()
    if websocket.headers.get("origin") not in settings.allowed_origins:
        await websocket.close(code=4403)
        return
    cookie = websocket.cookies.get(SESSION_COOKIE)

    def _auth() -> str | None:
        with session_scope() as s:
            return _authorize_ws(s, cookie, meeting_id)

    user_id = await anyio.to_thread.run_sync(_auth)
    if user_id is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        first = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
        after = int(first.get("after", 0))
    except (TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        after = 0
    try:
        recheck = 0
        while True:
            def _poll(cursor: int = after) -> list[dict[str, Any]]:
                with session_scope() as s:
                    return _fetch(s, meeting_id, cursor)

            events = await anyio.to_thread.run_sync(_poll)
            if events:
                after = events[-1]["id"]
                await websocket.send_text(json.dumps({"events": events}, ensure_ascii=False, default=str))
            recheck += 1
            if recheck % 100 == 0:  # ~30 s: session may have been revoked or access removed
                if await anyio.to_thread.run_sync(_auth) is None:
                    await websocket.close(code=4401)
                    return
            await asyncio.sleep(POLL_INTERVAL_S)
    except (WebSocketDisconnect, RuntimeError):
        return


def segment_dict(seg: TranscriptSegment) -> dict[str, Any]:
    return {"id": seg.id, "revision_id": seg.revision_id, "source_id": seg.source_key, "start_ms": seg.start_ms,
            "end_ms": seg.end_ms, "text": seg.text, "language": seg.language, "speaker_id": seg.speaker_id,
            "state": seg.state, "rev": seg.rev, "overlap": seg.overlap, "review_reasons": seg.review_reasons,
            "edited": seg.edited_by is not None}


@router.get("/meetings/{meeting_id}/live-state")
def live_state(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    meeting = meeting_for(db, user, meeting_id, "read")
    live_rev = db.scalar(select(TranscriptRevision).where(TranscriptRevision.meeting_id == meeting_id,
                                                          TranscriptRevision.kind == "live"))
    segments = []
    if live_rev is not None:
        segments = [segment_dict(s) for s in db.scalars(
            select(TranscriptSegment).where(TranscriptSegment.revision_id == live_rev.id)
            .order_by(TranscriptSegment.start_ms))]
    captures = db.scalars(select(CaptureSession).where(CaptureSession.meeting_id == meeting_id)
                          .order_by(CaptureSession.created_at.desc()).limit(1)).all()
    actions = db.scalars(select(ActionItem).where(ActionItem.meeting_id == meeting_id,
                                                  ActionItem.review_state != "rejected")
                         .order_by(ActionItem.number)).all()
    jobs = db.scalars(select(Job).where(Job.meeting_id == meeting_id).order_by(Job.created_at)).all()
    latest_metrics = {}
    for name in ("asr_backlog_s", "asr_rtf", "partial_latency_s", "stable_latency_s"):
        m = db.scalar(select(MetricSample).where(MetricSample.meeting_id == meeting_id, MetricSample.name == name)
                      .order_by(MetricSample.id.desc()).limit(1))
        if m is not None:
            latest_metrics[name] = {"value": m.value, "labels": m.labels, "at": m.at.isoformat()}
    return {
        "meeting_status": meeting.status,
        "last_event_id": _last_id(db, meeting_id),
        "capture": [{"id": c.id, "mode": c.mode, "state": c.state, "connected": c.connected,
                     "started_at": c.started_at, "last_frame_at": c.last_frame_at} for c in captures],
        "segments": segments,
        "preliminary_actions": [{"id": a.id, "action": a.action, "assignee": a.assignee, "deadline": a.deadline,
                                 "review_state": a.review_state, "origin": a.origin} for a in actions],
        "jobs": [{"id": j.id, "kind": j.kind, "state": j.state, "attempts": j.attempts, "progress": j.progress,
                  "last_error": j.last_error} for j in jobs],
        "metrics": latest_metrics,
    }
