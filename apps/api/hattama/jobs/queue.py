"""DB-backed job queue (see DECISIONS.md D-003: chosen instead of Celery/Redis).

Guarantees: idempotent enqueue by unique `key`; at-most-one running lease per job; expired leases are
recovered after a worker crash; bounded attempts; cooperative cancellation; state survives restarts
because it lives in the same transactional database as the data it produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hattama.db.models import Job, ResourceLock
from hattama.db.types import utcnow
from hattama.domain.enums import JobState


class JobCancelledError(RuntimeError):
    pass


def enqueue(
    session: Session,
    *,
    kind: str,
    key: str,
    queue: str = "pipeline",
    meeting_id: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job:
    existing = session.scalar(select(Job).where(Job.key == key))
    if existing is not None:
        return existing
    job = Job(kind=kind, key=key, queue=queue, meeting_id=meeting_id, payload=payload or {}, priority=priority,
              run_after=run_after or utcnow(), max_attempts=max_attempts)
    try:
        with session.begin_nested():
            session.add(job)
    except IntegrityError:
        found = session.scalar(select(Job).where(Job.key == key))
        assert found is not None
        return found
    return job


def claim(session: Session, *, queue: str, worker_id: str, lease_s: float = 120.0,
          kinds: list[str] | None = None) -> Job | None:
    now = utcnow()
    stmt = (
        select(Job)
        .where(Job.state == JobState.QUEUED, Job.queue == queue, Job.run_after <= now, Job.cancel_requested.is_(False))
        .order_by(Job.priority, Job.created_at)
        .limit(1)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = session.scalar(stmt)
    if job is None:
        return None
    job.state = JobState.RUNNING
    job.attempts += 1
    job.lease_owner = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_s)
    session.flush()
    return job


def heartbeat(session: Session, job_id: str, worker_id: str, lease_s: float = 120.0) -> bool:
    """Extend the lease. Returns False when the job was cancelled or the lease was lost."""
    res = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == worker_id, Job.state == JobState.RUNNING)
        .values(lease_expires_at=utcnow() + timedelta(seconds=lease_s))
    )
    if res.rowcount == 0:  # type: ignore[attr-defined]
        return False
    cancel = session.scalar(select(Job.cancel_requested).where(Job.id == job_id))
    return not cancel


def set_progress(session: Session, job_id: str, progress: dict[str, Any]) -> None:
    session.execute(update(Job).where(Job.id == job_id).values(progress=progress))


def complete(session: Session, job_id: str, worker_id: str, result: dict[str, Any] | None = None) -> None:
    session.execute(
        update(Job).where(Job.id == job_id, Job.lease_owner == worker_id)
        .values(state=JobState.SUCCEEDED, result=result or {}, finished_at=utcnow(), lease_owner=None,
                lease_expires_at=None, last_error=None)
    )


def fail(session: Session, job_id: str, worker_id: str, error: str, *, retryable: bool,
         retry_delay_s: float = 30.0) -> str:
    job = session.get(Job, job_id)
    if job is None or job.lease_owner != worker_id:
        return "lost"
    job.last_error = error[:4000]
    job.lease_owner = None
    job.lease_expires_at = None
    if job.cancel_requested:
        job.state = JobState.CANCELLED
        job.finished_at = utcnow()
    elif retryable and job.attempts < job.max_attempts:
        job.state = JobState.QUEUED
        job.run_after = utcnow() + timedelta(seconds=retry_delay_s * job.attempts)
    else:
        job.state = JobState.FAILED
        job.finished_at = utcnow()
    return str(job.state)


def recover_expired(session: Session) -> int:
    """Re-queue jobs whose worker died (lease expired). Bounded by max_attempts."""
    now = utcnow()
    jobs = session.scalars(select(Job).where(Job.state == JobState.RUNNING, Job.lease_expires_at < now)).all()
    for job in jobs:
        job.lease_owner = None
        job.lease_expires_at = None
        if job.cancel_requested:
            job.state = JobState.CANCELLED
            job.finished_at = now
        elif job.attempts >= job.max_attempts:
            job.state = JobState.FAILED
            job.last_error = (job.last_error or "") + " | lease expired (worker crashed?) after max attempts"
            job.finished_at = now
        else:
            job.state = JobState.QUEUED
    return len(jobs)


def request_cancel(session: Session, job_id: str) -> None:
    job = session.get(Job, job_id)
    if job is None:
        return
    job.cancel_requested = True
    if job.state == JobState.QUEUED:
        job.state = JobState.CANCELLED
        job.finished_at = utcnow()


@dataclass
class LockLease:
    name: str
    owner: str


def try_acquire_lock(session: Session, name: str, owner: str, ttl_s: float) -> bool:
    """Named lease lock (e.g. 'heavy' for exclusive heavy stages in the economy profile)."""
    now = utcnow()
    lock = session.get(ResourceLock, name)
    if lock is None:
        try:
            with session.begin_nested():
                session.add(ResourceLock(name=name, owner=owner, expires_at=now + timedelta(seconds=ttl_s)))
            return True
        except IntegrityError:
            lock = session.get(ResourceLock, name)
            if lock is None:
                return False
    if lock.owner in (None, owner) or (lock.expires_at is not None and lock.expires_at < now):
        res = session.execute(
            update(ResourceLock)
            .where(ResourceLock.name == name, ResourceLock.owner.is_(lock.owner) if lock.owner is None
                   else ResourceLock.owner == lock.owner)
            .values(owner=owner, expires_at=now + timedelta(seconds=ttl_s))
        )
        return bool(res.rowcount)  # type: ignore[attr-defined]
    return False


def release_lock(session: Session, name: str, owner: str) -> None:
    session.execute(update(ResourceLock).where(ResourceLock.name == name, ResourceLock.owner == owner)
                    .values(owner=None, expires_at=None))
