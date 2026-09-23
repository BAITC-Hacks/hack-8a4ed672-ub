from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from hattama.config import get_settings


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            dbapi_conn.isolation_level = None  # SQLAlchemy emits BEGIN itself (see _sqlite_begin)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # type: ignore[no-untyped-def]
            # IMMEDIATE avoids write-upgrade deadlocks between API and worker processes.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        engine.dialect._hattama_sqlite = True  # type: ignore[attr-defined]
        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine(get_settings().resolved_database_url())


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def reset_engine() -> None:
    try:
        get_engine().dispose()
    except Exception:  # noqa: BLE001 - disposal of a possibly broken engine
        pass
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def is_sqlite(session: Session) -> bool:
    return session.get_bind().dialect.name == "sqlite"
