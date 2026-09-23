"""ASR worker process: owns the ASR model; live streams first, final-pass jobs when live is idle."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time

from hattama.asr.base import ASRProvider, AsrUnavailableError
from hattama.audio.vad import SileroVad
from hattama.config import get_settings
from hattama.db.session import session_scope
from hattama.jobs.queue import recover_expired
from hattama.logging_setup import configure_logging, enforce_offline_env
from hattama.runtime import active_profile, make_asr_provider
from hattama.workers.live_asr import LiveAsrLoop

log = logging.getLogger("hattama.asr_worker")
IDLE_SLEEP_S = 0.2


def run(stop: threading.Event | None = None) -> int:
    configure_logging()
    enforce_offline_env()
    settings = get_settings()
    profile = active_profile(settings)
    stop = stop or threading.Event()
    worker_id = f"asr@{socket.gethostname()}:{os.getpid()}"
    try:
        live_provider: ASRProvider = make_asr_provider(settings, "live")
    except AsrUnavailableError as exc:
        log.error("ASR недоступен: %s", exc)
        return 2
    same = profile["final_asr"]["model"] == profile["live_asr"]["model"] and \
        profile["final_asr"]["device"] == profile["live_asr"]["device"] and \
        profile["final_asr"]["compute_type"] == profile["live_asr"]["compute_type"]
    final_provider = live_provider if same else None
    live = LiveAsrLoop(settings, live_provider, SileroVad(), profile["live_asr"],
                       analysis_enabled=bool(profile.get("live_analysis", True)))

    from hattama.pipeline.final_asr import FinalAsrRunner

    final = FinalAsrRunner(settings, lambda: final_provider or make_asr_provider(settings, "final"), SileroVad(),
                           profile["final_asr"], worker_id, yield_to_live=live.tick)
    log.info("asr worker started", extra={"worker": worker_id, "profile": settings.profile,
                                          "shared_model": same})
    last_recover = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() - last_recover > 30:
                with session_scope() as s:
                    recover_expired(s)
                last_recover = time.monotonic()
            worked = live.tick()
            if not worked:
                worked = final.tick()
            if not worked:
                stop.wait(IDLE_SLEEP_S)
        except AsrUnavailableError as exc:
            log.error("ASR error: %s", exc)
            stop.wait(5)
        except Exception:  # noqa: BLE001 - worker must survive one failed iteration; error is logged with trace
            log.exception("asr worker iteration failed")
            stop.wait(2)
    return 0


def main() -> int:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return run(stop)


if __name__ == "__main__":
    raise SystemExit(main())
