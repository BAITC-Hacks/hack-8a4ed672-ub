"""Internal reminders (in-app only; e-mail/EDMS are separate, explicitly configured integrations — not built).

Reminder policy is separate from the deadline wording: 09:00 local time one business day before, on the
day, and the day after (overdue). Only for a CONFIRMED calendar deadline; no 'overdue' for unknown dates.
Dedupe key = action + kind + deadline date, so a changed deadline cancels old reminders and creates new ones.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from hattama.db.models import ActionItem, Notification, Participant
from hattama.db.types import utcnow


def _prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _recipient(session: Session, a: ActionItem) -> str | None:
    if a.assignee_user_id:
        return a.assignee_user_id
    if a.assignee_participant_id:
        p = session.get(Participant, a.assignee_participant_id)
        return p.user_id if p else None
    return None


def plan(a: ActionItem) -> list[tuple[str, datetime]]:
    d = a.deadline or {}
    if not a.deadline_confirmed or not d.get("normalized_date") or a.conditional or a.on_hold:
        return []
    if a.execution_state not in ("open", "in_progress"):
        return []
    tz = ZoneInfo(d.get("timezone") or "Asia/Almaty")
    due = date.fromisoformat(d["normalized_date"])
    at9 = lambda day: datetime.combine(day, time(9, 0), tz)  # noqa: E731
    return [("due_soon", at9(_prev_business_day(due))), ("due_today", at9(due)),
            ("overdue", at9(due + timedelta(days=1)))]


def reschedule(session: Session, a: ActionItem) -> None:
    recipient = _recipient(session, a)
    wanted = {}
    if recipient:
        due = (a.deadline or {}).get("normalized_date")
        for kind, when in plan(a):
            wanted[f"{a.id}:{kind}:{due}"] = (kind, when)
    for n in session.scalars(select(Notification).where(Notification.action_id == a.id,
                                                        Notification.status == "scheduled")):
        if n.dedupe_key not in wanted:
            n.status = "cancelled"
    for key, (kind, when) in wanted.items():
        if session.scalar(select(Notification.id).where(Notification.dedupe_key == key)) is None:
            titles = {"due_soon": "Завтра срок поручения", "due_today": "Сегодня срок поручения",
                      "overdue": "Поручение просрочено"}
            session.add(Notification(dedupe_key=key, user_id=recipient, meeting_id=a.meeting_id, action_id=a.id,
                                     kind=kind, scheduled_for=when, title=titles[kind], body=a.action[:500]))


def deliver_due(session: Session) -> int:
    now = utcnow()
    delivered = 0
    for n in session.scalars(select(Notification).where(Notification.status == "scheduled",
                                                        Notification.scheduled_for <= now)):
        a = session.get(ActionItem, n.action_id) if n.action_id else None
        if a is None or not plan(a):  # done/cancelled/unconfirmed meanwhile -> no reminder
            n.status = "cancelled"
            continue
        n.status, n.delivered_at = "delivered", now
        delivered += 1
    return delivered
