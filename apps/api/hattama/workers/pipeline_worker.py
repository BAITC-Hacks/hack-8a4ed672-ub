"""Pipeline worker: finalize / diarize / extract_final / live_extract jobs (bounded, idempotent, recoverable)."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time

from hattama.config import get_settings
from hattama.db.session import session_scope
from hattama.jobs import queue
from hattama.llm.client import LlmOutputError, LlmUnavailableError
from hattama.logging_setup import configure_logging, enforce_offline_env
from hattama.pipeline import stages
from hattama.runtime import active_profile
from hattama.services.events import publish

log = logging.getLogger("hattama.pipeline_worker")
HEAVY = {"diarize", "extract_final"}


def handle(job_id: str, kind: str, meeting_id: str, payload: dict, worker_id: str, exclusive: bool) -> dict:
    settings = get_settings()
    if kind == "finalize_meeting":
        with session_scope() as s:
            return stages.stage_finalize(s, meeting_id, payload)
    if kind in HEAVY and exclusive:
        with session_scope() as s:
            if not queue.try_acquire_lock(s, "heavy", worker_id, ttl_s=1800):
                raise _Busy()
    try:
        if kind == "diarize":
            return stages.stage_diarize(settings, meeting_id, payload)
        if kind == "extract_final":
            return stages.stage_extract(settings, meeting_id, payload, live=False)
        if kind == "live_extract":
            return stages.stage_extract(settings, meeting_id, payload, live=True)
        raise ValueError(f"unknown job kind {kind}")
    finally:
        if kind in HEAVY and exclusive:
            with session_scope() as s:
                queue.release_lock(s, "heavy", worker_id)


class _Busy(Exception):
    pass


def run(stop: threading.Event) -> int:
    configure_logging()
    enforce_offline_env()
    settings = get_settings()
    exclusive = bool(active_profile(settings).get("heavy_stages_exclusive", True))
    worker_id = f"pipeline@{socket.gethostname()}:{os.getpid()}"
    log.info("pipeline worker started", extra={"worker": worker_id, "profile": settings.profile})
    last_recover = 0.0
    last_notify = 0.0
    while not stop.is_set():
        if time.monotonic() - last_recover > 30:
            with session_scope() as s:
                queue.recover_expired(s)
            last_recover = time.monotonic()
        if time.monotonic() - last_notify > 60:
            from hattama.notifications.scheduler import deliver_due

            with session_scope() as s:
                deliver_due(s)
            last_notify = time.monotonic()
        with session_scope() as s:
            job = queue.claim(s, queue="pipeline", worker_id=worker_id, lease_s=1800)
            info = (job.id, job.kind, job.meeting_id, dict(job.payload), job.attempts, job.max_attempts) if job else None
        if info is None:
            stop.wait(0.5)
            continue
        job_id, kind, meeting_id, payload, attempts, max_attempts = info
        try:
            result = handle(job_id, kind, meeting_id or "", payload, worker_id, exclusive)
            with session_scope() as s:
                queue.complete(s, job_id, worker_id, result)
                if meeting_id:
                    publish(s, meeting_id, "job.state", {"kind": kind, "state": "succeeded"})
        except _Busy:
            with session_scope() as s:
                queue.fail(s, job_id, worker_id, "heavy stage busy", retryable=True, retry_delay_s=10)
        except (LlmUnavailableError, LlmOutputError) as exc:
            with session_scope() as s:
                state = queue.fail(s, job_id, worker_id, str(exc), retryable=True, retry_delay_s=60)
            if state == "failed" and kind == "extract_final" and meeting_id:
                stages.mark_llm_failed(meeting_id, str(exc))
            log.warning("job %s (%s) LLM error: %s -> %s", job_id, kind, exc, state)
        except Exception as exc:  # noqa: BLE001 - job boundary: record the failure with trace, keep worker alive
            log.exception("job %s (%s) failed", job_id, kind)
            with session_scope() as s:
                queue.fail(s, job_id, worker_id, f"{type(exc).__name__}: {exc}", retryable=attempts < max_attempts)
    return 0


def main() -> int:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return run(stop)


if __name__ == "__main__":
    raise SystemExit(main())
