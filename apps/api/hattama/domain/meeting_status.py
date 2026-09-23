from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from hattama.db.models import Meeting
from hattama.domain.enums import MeetingStatus as S

ALLOWED: dict[str, set[str]] = {
    S.DRAFT: {S.READY},
    S.READY: {S.LIVE, S.PROCESSING, S.DRAFT},
    S.LIVE: {S.PROCESSING, S.READY, S.FAILED},
    S.PROCESSING: {S.NEEDS_REVIEW, S.FAILED, S.LIVE},
    S.NEEDS_REVIEW: {S.APPROVED, S.PROCESSING},
    S.APPROVED: {S.NEEDS_REVIEW},
    S.FAILED: {S.PROCESSING, S.NEEDS_REVIEW},
}


class StatusTransitionError(ValueError):
    pass


def transition(session: Session, meeting_id: str, target: str, *, only_from: set[str] | None = None) -> bool:
    """Atomic status change (compare-and-set). Bumps the optimistic version.

    Returns False if the meeting is not in an allowed source status (no-op, not an error),
    so concurrent workers can call it idempotently.
    """
    sources = {s for s, targets in ALLOWED.items() if target in targets}
    if only_from is not None:
        sources &= only_from
    if not sources:
        raise StatusTransitionError(f"no status may transition to {target}")
    res = session.execute(
        update(Meeting)
        .where(Meeting.id == meeting_id, Meeting.status.in_(sources))
        .values(status=target, version=Meeting.version + 1)
        .execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]
