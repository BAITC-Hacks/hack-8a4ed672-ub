from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hattama.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Principal,
    client_ip,
    current_principal,
    get_db,
    rate_limiter,
)
from hattama.config import Settings, get_settings
from hattama.db.models import AuthSession, User
from hattama.db.types import utcnow
from hattama.security.passwords import verify_password
from hattama.security.tokens import hash_token, new_token
from hattama.services.events import audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role)


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)) -> SessionOut:
    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недопустимый Origin")
    rate_limiter.check(f"login:{client_ip(request)}", settings.login_rate_per_minute)
    rate_limiter.check(f"login-user:{body.email.lower()}", settings.login_rate_per_minute)
    user = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if not verify_password(user.password_hash if user else None, body.password) or user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль")
    token = new_token()
    csrf = new_token(24)
    db.add(AuthSession(user_id=user.id, token_hash=hash_token(token), csrf_token=csrf,
                       expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours)))
    audit(db, action="auth.login", actor_user_id=user.id)
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=settings.cookie_secure,
                        samesite="strict", path="/")
    return SessionOut(user=_user_out(user), csrf_token=csrf)


@router.post("/logout", status_code=204)
def logout(response: Response, principal: Principal = Depends(current_principal),
           db: Session = Depends(get_db)) -> Response:
    auth = db.get(AuthSession, principal.session.id)
    if auth is not None:
        auth.revoked_at = utcnow()
    audit(db, action="auth.logout", actor_user_id=principal.user.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=SessionOut)
def me(principal: Principal = Depends(current_principal)) -> SessionOut:
    return SessionOut(user=_user_out(principal.user), csrf_token=principal.session.csrf_token)
