"""Live events (DB-backed bus between workers/API and dashboard WebSockets) and audit trail."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from hattama.db.models import AuditEvent, LiveEvent, MetricSample


def publish(session: Session, meeting_id: str, type_: str, payload: dict[str, Any]) -> None:
    session.add(LiveEvent(meeting_id=meeting_id, type=type_, payload=payload))


def audit(
    session: Session,
    *,
    action: str,
    actor_user_id: str | None,
    actor_kind: str = "user",
    meeting_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Audit records hold field values of edited objects but never raw audio or tokens."""
    session.add(AuditEvent(action=action, actor_user_id=actor_user_id, actor_kind=actor_kind, meeting_id=meeting_id,
                           object_type=object_type, object_id=object_id, before=before, after=after))


def metric(session: Session, name: str, value: float, meeting_id: str | None = None, **labels: Any) -> None:
    session.add(MetricSample(meeting_id=meeting_id, name=name, value=float(value), labels=labels))
