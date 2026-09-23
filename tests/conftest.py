"""Shared fixtures. Every test gets an isolated data dir and SQLite database; nothing touches user data."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
WEB_ORIGIN = "http://localhost:5173"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("HATTAMA_ENV", "test")
    monkeypatch.setenv("HATTAMA_DATA_DIR", str(data))
    monkeypatch.setenv("HATTAMA_DATABASE_URL", f"sqlite:///{(data / 'test.sqlite3').as_posix()}")
    monkeypatch.setenv("HATTAMA_ALLOWED_EXTENSION_IDS", f'["{EXT_ID}"]')
    monkeypatch.setenv("HATTAMA_PROFILE", os.environ.get("HATTAMA_TEST_PROFILE", "test"))
    monkeypatch.setenv("HATTAMA_FSYNC_INTERVAL_S", "0.05")
    monkeypatch.setenv("HATTAMA_STOP_GRACE_S", "2")
    from hattama.api.deps import rate_limiter
    from hattama.config import reset_settings_cache
    from hattama.db.session import reset_engine

    reset_settings_cache()
    reset_engine()
    rate_limiter.reset()
    yield data
    reset_engine()
    reset_settings_cache()


@pytest.fixture()
def db_schema(isolated_env: Path) -> None:
    from hattama.db.models import Base
    from hattama.db.session import get_engine

    Base.metadata.create_all(get_engine())


@dataclass
class LoggedIn:
    client: object
    user_id: str
    csrf: str
    email: str

    def headers(self) -> dict[str, str]:
        return {"x-csrf-token": self.csrf, "origin": WEB_ORIGIN}

    def ws_headers(self, origin: str = WEB_ORIGIN) -> dict[str, str]:
        """TestClient does not attach cookies to WebSocket handshakes (browsers do): pass them explicitly."""
        jar = "; ".join(f"{k}={v}" for k, v in self.client.cookies.items())  # type: ignore[attr-defined]
        return {"origin": origin, "cookie": jar}


def make_user(email: str, role: str, name: str = "Тест", password: str = "correct-horse-battery") -> str:
    from hattama.db.models import User
    from hattama.db.session import session_scope
    from hattama.security.passwords import hash_password

    with session_scope() as s:
        u = User(email=email, display_name=name, role=role, password_hash=hash_password(password))
        s.add(u)
        s.flush()
        return u.id


@pytest.fixture()
def app(db_schema: None):  # type: ignore[no-untyped-def]
    from hattama.api.app import create_app

    return create_app()


def login(app, email: str, password: str = "correct-horse-battery") -> LoggedIn:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    client = TestClient(app, base_url="http://localhost:8000")
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password},
                    headers={"origin": WEB_ORIGIN})
    assert r.status_code == 200, r.text
    body = r.json()
    return LoggedIn(client, body["user"]["id"], body["csrf_token"], email)


@pytest.fixture()
def secretary(app) -> LoggedIn:  # type: ignore[no-untyped-def]
    make_user("sec@example.test", "secretary", "Секретарь")
    return login(app, "sec@example.test")


def create_meeting(user: LoggedIn, **overrides) -> dict:  # type: ignore[no-untyped-def]
    body = {"title": "Планёрка по закупкам", "meeting_date": "2026-10-01", "timezone": "Asia/Almaty",
            "language_mode": "mixed", "platform": "google_meet",
            "participants": [{"display_name": "Ботагоз"}, {"display_name": "Ерлан", "is_present": False},
                             {"display_name": "Секретарь", "is_self": True}]}
    body.update(overrides)
    r = user.client.post("/api/v1/meetings", json=body, headers=user.headers())  # type: ignore[attr-defined]
    assert r.status_code == 201, r.text
    return r.json()


def confirm_consent(user: LoggedIn, meeting_id: str) -> None:
    r = user.client.post(f"/api/v1/meetings/{meeting_id}/consent",  # type: ignore[attr-defined]
                         json={"confirmed": True}, headers=user.headers())
    assert r.status_code == 200, r.text
