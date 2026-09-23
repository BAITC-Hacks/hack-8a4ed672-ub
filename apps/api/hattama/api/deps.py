"""Request dependencies: DB session, authentication (HttpOnly cookie), CSRF, object-level access."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from hattama.config import Settings, get_settings
from hattama.db.models import ActionItem, AuthSession, Meeting, MeetingMember, Participant, User
from hattama.db.session import get_sessionmaker
from hattama.db.types import utcnow
from hattama.domain.enums import MemberRole, UserRole
from hattama.security.tokens import constant_time_equals, hash_token

SESSION_COOKIE = "hattama_session"
CSRF_COOKIE = "hattama_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

Access = Literal["read", "edit", "capture", "approve", "delete"]


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass
class Principal:
    user: User
    session: AuthSession


def _load_principal(request: Request, db: Session) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    auth = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if auth is None or auth.revoked_at is not None or auth.expires_at < utcnow():
        return None
    user = db.get(User, auth.user_id)
    if user is None or not user.is_active:
        return None
    return Principal(user, auth)


def current_principal(request: Request, db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> Principal:
    principal = _load_principal(request, db)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    if request.method in UNSAFE_METHODS:
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not constant_time_equals(header, principal.session.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF-токен отсутствует или неверен")
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недопустимый Origin")
    return principal


def current_user(principal: Principal = Depends(current_principal)) -> User:
    return principal.user


def require_role(*roles: str):  # type: ignore[no-untyped-def]
    def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
        return user

    return _dep


def member_role(db: Session, meeting_id: str, user_id: str) -> str | None:
    return db.scalar(select(MeetingMember.role).where(MeetingMember.meeting_id == meeting_id,
                                                      MeetingMember.user_id == user_id))


def meeting_for(db: Session, user: User, meeting_id: str, access: Access) -> Meeting:
    """Object-level authorization for every meeting-scoped endpoint. Unknown and forbidden look the same."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None or meeting.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Встреча не найдена")
    role = member_role(db, meeting_id, user.id)
    allowed = {
        "read": {MemberRole.SECRETARY, MemberRole.MANAGER, MemberRole.VIEWER},
        "edit": {MemberRole.SECRETARY},
        "capture": {MemberRole.SECRETARY},
        "approve": {MemberRole.SECRETARY},
        "delete": {MemberRole.SECRETARY},
    }[access]
    if role not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Встреча не найдена")
    return meeting


def can_view_action(db: Session, user: User, action: ActionItem) -> bool:
    if member_role(db, action.meeting_id, user.id) is not None:
        return True
    if action.assignee_user_id == user.id:
        return True
    if action.assignee_participant_id:
        participant = db.get(Participant, action.assignee_participant_id)
        return participant is not None and participant.user_id == user.id
    return False


def can_update_execution(db: Session, user: User, action: ActionItem) -> bool:
    role = member_role(db, action.meeting_id, user.id)
    if role in (MemberRole.SECRETARY, MemberRole.MANAGER):
        return True
    return can_view_action(db, user, action) and user.role in (UserRole.ASSIGNEE, UserRole.MANAGER,
                                                               UserRole.SECRETARY)


class RateLimiter:
    """In-process sliding window (single API process; documented). Keyed by client IP + bucket."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_s: float = 60.0) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > window_s:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много попыток, повторите позже")
        hits.append(now)

    def reset(self) -> None:
        self._hits.clear()


rate_limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
