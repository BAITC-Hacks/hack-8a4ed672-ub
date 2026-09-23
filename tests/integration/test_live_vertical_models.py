"""Vertical route with the REAL local ASR model: synthetic audio -> protocol v1 -> durable storage ->
live ASR worker loop -> stable segments in the DB and live events.

Marked `models`: skipped with an explicit NOT_RUN reason when the model is not prepared locally.
Audio is SYNTHETIC (Windows TTS); this is not a proof of Meet/Teams/Zoom integration.
"""

from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from conftest import confirm_consent, create_meeting
from hattama.db.models import LiveEvent, SourceEpoch, TranscriptSegment
from hattama.db.session import session_scope
from hattama.evaluation.metrics import term_recall, wer
from helpers.ingest import EXT_ORIGIN, hello, pcm_frame, recv_until, source_open, wait_ack

pytestmark = [pytest.mark.integration, pytest.mark.models]
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "audio" / "synthetic"


@pytest.fixture()
def gpu_profile(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from hattama.config import get_settings, reset_settings_cache
    from hattama.modelstore.manifest import model_status

    monkeypatch.setenv("HATTAMA_PROFILE", "gpu-6gb")
    reset_settings_cache()
    settings = get_settings()
    from hattama.modelstore.manifest import load_manifest

    entry = load_manifest(settings.model_manifest)["whisper-large-v3-turbo-ct2"]
    if not model_status(entry, settings.models_dir)["prepared"]:
        pytest.skip("NOT_RUN: whisper-large-v3-turbo-ct2 не подготовлена (python tasks.py models-prepare)")
    return settings


def _load(name: str) -> tuple[np.ndarray, dict]:
    with wave.open(str(FIX / f"{name}.wav"), "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768
    return audio, json.loads((FIX / f"{name}.ref.json").read_text(encoding="utf-8"))


def _to_48k(audio16: np.ndarray) -> np.ndarray:
    t16 = np.arange(len(audio16)) / 16000
    t48 = np.arange(len(audio16) * 3) / 48000
    return np.interp(t48, t16, audio16).astype(np.float32)


def test_synthetic_dialog_live_transcription(secretary, gpu_profile, isolated_env) -> None:  # type: ignore[no-untyped-def]
    from hattama.asr.base import AsrUnavailableError
    from hattama.audio.vad import SileroVad
    from hattama.runtime import active_profile, make_asr_provider
    from hattama.workers.live_asr import LiveAsrLoop

    try:
        provider = make_asr_provider(gpu_profile, "live")
    except AsrUnavailableError as exc:
        pytest.skip(f"NOT_RUN: {exc}")
    live = LiveAsrLoop(gpu_profile, provider, SileroVad(), active_profile(gpu_profile)["live_asr"],
                       analysis_enabled=False)
    audio16, ref = _load("ru_synth_dialog_b")
    audio = _to_48k(audio16)
    meeting = create_meeting(secretary)
    confirm_consent(secretary, meeting["id"])
    cs = secretary.client.post(f"/api/v1/meetings/{meeting['id']}/capture-sessions", json={"mode": "companion"},
                               headers=secretary.headers()).json()
    token = secretary.client.post("/api/v1/pairing/exchange", json={"code": cs["pairing"]["code"]},
                                  headers={"origin": EXT_ORIGIN}).json()["token"]
    start_us = int(time.time() * 1e6)
    chunk = 4800
    step_frames = 5  # 0.5 s of audio between worker ticks
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token=token))
        ws.receive_json()
        ws.send_text(source_open(start_us=start_us))
        idx = recv_until(ws, lambda m: m["type"] == "source_ready")["source_index"]
        seq = 0
        for pos in range(0, len(audio), chunk):
            ws.send_bytes(pcm_frame(idx, seq, pos, audio[pos:pos + chunk], start_us=start_us))
            seq += 1
            if seq % step_frames == 0:
                wait_ack(ws, seq - 1)
                live.tick()
        wait_ack(ws, seq - 1)
        ws.send_text(json.dumps({"type": "stop_session", "reason": "user_stop"}))
        ws.send_text(json.dumps({"type": "source_close", "source_index": idx, "capture_epoch": 0,
                                 "final_sequence": seq - 1, "reason": "stop_requested"}))
        recv_until(ws, lambda m: m["type"] == "session_stopped")
    for _ in range(50):
        live.tick()
        with session_scope() as s:
            if all(e.live_done for e in s.scalars(select(SourceEpoch))):
                break
    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment).order_by(TranscriptSegment.start_ms)).all()
        texts = [x.text for x in segs]
        starts = [x.start_ms for x in segs]
        partials = s.scalars(select(LiveEvent).where(LiveEvent.type == "segment.partial")).all()
    hyp = " ".join(texts)
    score = wer(ref["reference_text"], hyp)
    found, total, missing = term_recall(["Ботагоз", "Ерлан", "претензию", "поставщика", "расторгаем"], hyp)
    report = {"wer": round(score, 3), "segments": len(segs), "partial_events": len(partials),
              "terms": f"{found}/{total}", "missing_terms": missing}
    print("LIVE_VERTICAL_RESULT", json.dumps(report, ensure_ascii=False))
    print("HYPOTHESIS", hyp)
    assert segs, "no stable segments were produced"
    assert partials, "no partial (draft) events were published before stable text"
    assert starts == sorted(starts) and starts[-1] <= ref["duration_s"] * 1000 + 500
    assert score < 0.35, report
    assert found >= total - 1, report
