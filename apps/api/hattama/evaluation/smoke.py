"""python tasks.py smoke-local [--with-models] [--audio NAME]

Full local route in ONE process with external egress blocked (only loopback allowed):
synthetic audio -> protocol v1 over WebSocket -> durable storage (ACK after fsync) -> live ASR ->
stop -> final ASR -> speakers -> LLM extraction -> NEEDS_REVIEW -> PDF + DOCX export.
Without --with-models it stops after ingestion (no models needed) and says so.
Writes reports/runs/smoke-<timestamp>.json. Audio is SYNTHETIC (Windows TTS fixtures).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from hattama.config import REPO_ROOT


def _setup_env(tmp: Path, with_models: bool) -> None:
    os.environ["HATTAMA_DATA_DIR"] = str(tmp / "data")
    os.environ["HATTAMA_DATABASE_URL"] = f"sqlite:///{(tmp / 'smoke.sqlite3').as_posix()}"
    os.environ.setdefault("HATTAMA_ALLOWED_EXTENSION_IDS", '["bibicplbpcephoocaemgdjjbbhfhfion"]')
    os.environ["HATTAMA_FSYNC_INTERVAL_S"] = "0.05"
    if not with_models:
        os.environ["HATTAMA_PROFILE"] = "test"


def run(with_models: bool, audio_name: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="hattama-smoke-"))
    _setup_env(tmp, with_models)
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from hattama.config import get_settings, reset_settings_cache
    from hattama.db.models import ActionItem, Base, Meeting, User
    from hattama.db.session import get_engine, reset_engine, session_scope
    from hattama.security.egress_guard import block_external_egress
    from hattama.security.passwords import hash_password

    reset_settings_cache()
    reset_engine()
    settings = get_settings()
    Base.metadata.create_all(get_engine())
    report: dict = {"with_models": with_models, "profile": settings.profile, "audio": audio_name, "synthetic": True,
                    "steps": []}

    def step(name: str, **info) -> None:  # type: ignore[no-untyped-def]
        report["steps"].append({"step": name, "t": round(time.time() - t0, 2), **info})
        print(f"[smoke] {name} {json.dumps(info, ensure_ascii=False)}", flush=True)

    t0 = time.time()
    with block_external_egress() as attempts:
        from hattama.api.app import create_app

        with session_scope() as s:
            s.add(User(email="smoke@local", display_name="Секретарь", role="secretary",
                       password_hash=hash_password("smoke-password-123")))
        client = TestClient(create_app(), base_url="http://localhost:8000")
        origin = {"origin": "http://localhost:5173"}
        csrf = client.post("/api/v1/auth/login", json={"email": "smoke@local", "password": "smoke-password-123"},
                           headers=origin).json()["csrf_token"]
        h = {**origin, "x-csrf-token": csrf}
        meeting = client.post("/api/v1/meetings", headers=h, json={
            "title": "Смоук: поставки кабеля", "meeting_date": "2026-09-24", "timezone": "Asia/Almaty",
            "language_mode": "mixed", "platform": "google_meet",
            "participants": [{"display_name": "Руководитель"}, {"display_name": "Ботагоз"},
                             {"display_name": "Ерлан", "is_present": False}]}).json()
        mid = meeting["id"]
        client.post(f"/api/v1/meetings/{mid}/consent", headers=h, json={"confirmed": True})
        cs = client.post(f"/api/v1/meetings/{mid}/capture-sessions", headers=h, json={"mode": "companion"}).json()
        ext = {"origin": "chrome-extension://bibicplbpcephoocaemgdjjbbhfhfion"}
        token = client.post("/api/v1/pairing/exchange", json={"code": cs["pairing"]["code"]}, headers=ext).json()["token"]
        step("paired", capture_session=cs["id"])

        with wave.open(str(REPO_ROOT / "fixtures" / "audio" / "synthetic" / f"{audio_name}.wav"), "rb") as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768
        live = None
        if with_models:
            from hattama.audio.vad import SileroVad
            from hattama.runtime import active_profile, make_asr_provider
            from hattama.workers.live_asr import LiveAsrLoop

            provider = make_asr_provider(settings, "live")
            live = LiveAsrLoop(settings, provider, SileroVad(), active_profile(settings)["live_asr"],
                               analysis_enabled=False)
        sys.path.insert(0, str(REPO_ROOT / "tests"))
        from helpers.ingest import hello, pcm_frame, recv_until, source_open, wait_ack

        start_us = int(time.time() * 1e6)
        with client.websocket_connect("/api/v1/ingest/ws", headers=ext) as ws:
            ws.send_text(hello(token=token))
            ws.receive_json()
            ws.send_text(source_open(rate=16000, start_us=start_us))
            idx = recv_until(ws, lambda m: m["type"] == "source_ready")["source_index"]
            seq = 0
            for pos in range(0, len(audio), 1600):
                ws.send_bytes(pcm_frame(idx, seq, pos, audio[pos:pos + 1600], rate=16000, start_us=start_us))
                seq += 1
                if live is not None and seq % 5 == 0:
                    wait_ack(ws, seq - 1)
                    live.tick()
            ack = wait_ack(ws, seq - 1)
            step("streamed", seconds=round(len(audio) / 16000, 1), durable_samples=ack["durable_sample"])
            ws.send_text(json.dumps({"type": "stop_session", "reason": "user_stop"}))
            ws.send_text(json.dumps({"type": "source_close", "source_index": idx, "capture_epoch": 0,
                                     "final_sequence": seq - 1, "reason": "stop_requested"}))
            recv_until(ws, lambda m: m["type"] == "session_stopped")
        step("stopped", status=client.get(f"/api/v1/meetings/{mid}", headers=h).json()["status"])
        if not with_models:
            report["result"] = "INGEST_ONLY (модели не запускались: используйте --with-models)"
            report["egress_attempts"] = attempts
            return report
        for _ in range(30):
            live.tick()  # type: ignore[union-attr]
        state = client.get(f"/api/v1/meetings/{mid}/live-state", headers=h).json()
        step("live_asr", stable_segments=len(state["segments"]))

        from hattama.pipeline import stages
        from hattama.pipeline.final_asr import FinalAsrRunner
        from hattama.workers import pipeline_worker

        pipeline_worker_id = "smoke-pipeline"
        from hattama.jobs import queue as q

        def drain_pipeline() -> None:
            while True:
                with session_scope() as s:
                    job = q.claim(s, queue="pipeline", worker_id=pipeline_worker_id)
                    info = (job.id, job.kind, job.meeting_id, dict(job.payload)) if job else None
                if info is None:
                    return
                res = pipeline_worker.handle(info[0], info[1], info[2], info[3], pipeline_worker_id, True)
                with session_scope() as s:
                    q.complete(s, info[0], pipeline_worker_id, res)
                step(f"job:{info[1]}", result={k: v for k, v in res.items() if k != "raw_events"})

        drain_pipeline()  # finalize -> enqueue final_asr
        final = FinalAsrRunner(settings, lambda: provider, SileroVad(), {"beam_size": 5}, "smoke-asr")
        while final.tick():
            pass
        step("final_asr_done")
        drain_pipeline()  # diarize -> extract_final
        with session_scope() as s:
            m = s.get(Meeting, mid)
            actions = s.scalars(select(ActionItem).where(ActionItem.meeting_id == mid)).all()
            report["actions"] = [{"action": a.action, "assignee": (a.assignee or {}).get("name"),
                                  "deadline": (a.deadline or {}).get("normalized_date"),
                                  "deadline_raw": (a.deadline or {}).get("raw_text"), "review_state": a.review_state,
                                  "reasons": a.review_reasons} for a in actions]
            status = m.status if m else None
        step("analysis", meeting_status=status, actions=len(report["actions"]))
        for fmt in ("pdf", "docx"):
            ex = client.post(f"/api/v1/meetings/{mid}/exports", headers=h, json={"format": fmt}).json()
            blob = client.get(ex["download"], headers=h)
            step(f"export_{fmt}", draft=ex["draft"], bytes=len(blob.content), http=blob.status_code)
        report["egress_attempts"] = attempts
        report["result"] = "PASS" if status == "NEEDS_REVIEW" and not attempts else "FAIL"
        _ = stages
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-models", action="store_true")
    parser.add_argument("--audio", default="ru_synth_dialog_b")
    args = parser.parse_args(argv)
    report = run(args.with_models, args.audio)
    out = REPO_ROOT / "reports" / "runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[smoke] result: {report.get('result')}  report: {path}")
    return 0 if report.get("result", "").startswith(("PASS", "INGEST_ONLY")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
