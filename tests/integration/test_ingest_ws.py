from __future__ import annotations

import json
import time

import numpy as np
import pytest
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from conftest import EXT_ID, WEB_ORIGIN, confirm_consent, create_meeting, login, make_user
from hattama.db.models import AudioGap, CaptureSession, Job, Meeting, SourceEpoch
from hattama.db.session import session_scope
from hattama.ingest.store import read_pcm_range
from helpers.ingest import EXT_ORIGIN, hello, pcm_frame, recv_until, source_open, wait_ack

pytestmark = pytest.mark.integration
RATE = 48000
CHUNK = 4800  # 100 ms


def _pair(sec) -> tuple[str, str, str]:  # type: ignore[no-untyped-def]
    meeting = create_meeting(sec)
    confirm_consent(sec, meeting["id"])
    r = sec.client.post(f"/api/v1/meetings/{meeting['id']}/capture-sessions", json={"mode": "companion"},
                        headers=sec.headers())
    assert r.status_code == 201, r.text
    cs = r.json()
    r = sec.client.post("/api/v1/pairing/exchange", json={"code": cs["pairing"]["code"]},
                        headers={"origin": EXT_ORIGIN})
    assert r.status_code == 200, r.text
    return meeting["id"], cs["id"], r.json()["token"]


def _signal(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-0.3, 0.3, n).astype(np.float32)


def test_extension_stream_ack_stop_and_finalize_once(secretary, isolated_env) -> None:  # type: ignore[no-untyped-def]
    meeting_id, cs_id, token = _pair(secretary)
    start_us = int(time.time() * 1e6)
    chunks = [_signal(CHUNK, i) for i in range(10)]
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token=token))
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome" and welcome["limits"]["ack_level"] == "fsync"
        ws.send_text(source_open(start_us=start_us))
        ready = recv_until(ws, lambda m: m["type"] == "source_ready")
        assert ready["resume_from_sequence"] == 0
        idx = ready["source_index"]
        for i, c in enumerate(chunks):
            ws.send_bytes(pcm_frame(idx, i, i * CHUNK, c, start_us=start_us))
        ack = wait_ack(ws, 9)
        assert ack["durable_sample"] == 10 * CHUNK
        # duplicate resend is acknowledged idempotently and does not change data
        ws.send_bytes(pcm_frame(idx, 3, 3 * CHUNK, chunks[3], start_us=start_us))
        ws.send_text(json.dumps({"type": "stop_session", "reason": "user_stop"}))
        ws.send_text(json.dumps({"type": "source_close", "source_index": idx, "capture_epoch": 0,
                                 "final_sequence": 9, "reason": "stop_requested"}))
        recv_until(ws, lambda m: m["type"] == "source_closed")
        recv_until(ws, lambda m: m["type"] == "session_stopped")
    with session_scope() as s:
        cs = s.get(CaptureSession, cs_id)
        assert cs is not None and cs.state == "STOPPED"
        assert s.get(Meeting, meeting_id).status == "PROCESSING"  # type: ignore[union-attr]
        jobs = s.scalars(select(Job).where(Job.meeting_id == meeting_id, Job.kind == "finalize_meeting")).all()
        assert len(jobs) == 1
        epoch = s.scalars(select(SourceEpoch)).one()
        assert epoch.durable_sequence == 9 and epoch.durable_samples == 10 * CHUNK
        assert epoch.duplicate_frames == 1 and epoch.closed_at is not None
        path = isolated_env / epoch.rel_path
    stored = read_pcm_range(path, 1, 0, 10 * CHUNK)
    expected = np.concatenate([(np.clip(c, -1, 1) * 32767).astype("<i2").astype(np.float32) / 32768 for c in chunks])
    assert np.allclose(stored, expected)
    # stopping again is a no-op, finalization is not enqueued twice
    r = secretary.client.post(f"/api/v1/capture-sessions/{cs_id}/stop", headers=secretary.headers())
    assert r.status_code == 200
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(Job.kind == "finalize_meeting")) == 1


def test_reconnect_resumes_from_durable_sequence(secretary) -> None:  # type: ignore[no-untyped-def]
    _, _, token = _pair(secretary)
    start_us = int(time.time() * 1e6)
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token=token))
        ws.receive_json()
        ws.send_text(source_open(start_us=start_us))
        idx = recv_until(ws, lambda m: m["type"] == "source_ready")["source_index"]
        for i in range(3):
            ws.send_bytes(pcm_frame(idx, i, i * CHUNK, _signal(CHUNK, i), start_us=start_us))
        wait_ack(ws, 2)
    # network drop: new connection, same epoch -> server tells where to resume
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token=token))
        ws.receive_json()
        ws.send_text(source_open(start_us=start_us))
        ready = recv_until(ws, lambda m: m["type"] == "source_ready")
        assert ready["resume_from_sequence"] == 3 and ready["durable_sequence"] == 2
        for i in range(2, 6):  # 2 is a resend of an already durable frame
            ws.send_bytes(pcm_frame(idx, i, i * CHUNK, _signal(CHUNK, i), start_us=start_us))
        assert wait_ack(ws, 5)["durable_sample"] == 6 * CHUNK


def test_sequence_gap_is_recorded_not_hidden(secretary) -> None:  # type: ignore[no-untyped-def]
    meeting_id, _, token = _pair(secretary)
    start_us = int(time.time() * 1e6)
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token=token))
        ws.receive_json()
        ws.send_text(source_open(start_us=start_us))
        idx = recv_until(ws, lambda m: m["type"] == "source_ready")["source_index"]
        ws.send_bytes(pcm_frame(idx, 0, 0, _signal(CHUNK, 0), start_us=start_us))
        ws.send_bytes(pcm_frame(idx, 2, 2 * CHUNK, _signal(CHUNK, 2), start_us=start_us))  # seq 1 lost
        ws.send_text(json.dumps({"type": "gap_report", "source_index": idx, "capture_epoch": 0, "from_sequence": 1,
                                 "to_sequence": 1, "start_sample": CHUNK, "lost_samples": CHUNK,
                                 "reason": "client_buffer_overflow"}))
        gap = recv_until(ws, lambda m: m["type"] == "gap_recorded")
        assert (gap["start_sample"], gap["end_sample"]) == (CHUNK, 2 * CHUNK)
        assert wait_ack(ws, 2)["durable_sample"] == 3 * CHUNK
    with session_scope() as s:
        gaps = s.scalars(select(AudioGap)).all()
        assert len(gaps) == 1 and gaps[0].reason == "client_buffer_overflow"


def test_rejects_unknown_origin_bad_token_and_missing_consent(secretary) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(WebSocketDisconnect) as err, \
            secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": "https://evil.example"}) as ws:
        ws.receive_json()
    assert err.value.code == 4403
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token="forged-token"))
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "unauthorized"
    meeting = create_meeting(secretary)
    r = secretary.client.post(f"/api/v1/meetings/{meeting['id']}/capture-sessions", json={"mode": "companion"},
                              headers=secretary.headers())
    assert r.status_code == 409  # consent first


def test_other_secretary_cannot_stream_into_foreign_meeting(app, secretary) -> None:  # type: ignore[no-untyped-def]
    meeting = create_meeting(secretary)
    confirm_consent(secretary, meeting["id"])
    r = secretary.client.post(f"/api/v1/meetings/{meeting['id']}/capture-sessions", json={"mode": "local_mic"},
                              headers=secretary.headers())
    cs_id = r.json()["id"]
    make_user("other@example.test", "secretary")
    other = login(app, "other@example.test")
    with other.client.websocket_connect("/api/v1/ingest/ws", headers=other.ws_headers()) as ws:
        ws.send_text(hello(capture_session_id=cs_id))
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "forbidden"
    assert other.client.get(f"/api/v1/meetings/{meeting['id']}").status_code == 404
    assert other.client.post(f"/api/v1/capture-sessions/{cs_id}/stop", headers=other.headers()).status_code == 404


def test_local_microphone_via_cookie_and_rest_stop_notifies_client(secretary) -> None:  # type: ignore[no-untyped-def]
    meeting = create_meeting(secretary, platform="in_person")
    confirm_consent(secretary, meeting["id"])
    cs = secretary.client.post(f"/api/v1/meetings/{meeting['id']}/capture-sessions", json={"mode": "local_mic"},
                               headers=secretary.headers()).json()
    start_us = int(time.time() * 1e6)
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers=secretary.ws_headers()) as ws:
        ws.send_text(hello(capture_session_id=cs["id"]))
        assert ws.receive_json()["type"] == "welcome"
        ws.send_text(source_open("mic", "tab_audio", start_us=start_us))
        assert recv_until(ws, lambda m: m["type"] in ("error", "source_ready"))["code"] == "bad_source_kind"
        ws.send_text(source_open("mic", "local_microphone", 16000, start_us=start_us))
        idx = recv_until(ws, lambda m: m["type"] == "source_ready")["source_index"]
        ws.send_bytes(pcm_frame(idx, 0, 0, _signal(1600, 1), rate=16000, start_us=start_us))
        wait_ack(ws, 0)
        r = secretary.client.post(f"/api/v1/capture-sessions/{cs['id']}/stop", headers=secretary.headers())
        assert r.json()["state"] == "STOPPING"
        recv_until(ws, lambda m: m["type"] == "stop_requested")
        ws.send_text(json.dumps({"type": "source_close", "source_index": idx, "capture_epoch": 0,
                                 "final_sequence": 0, "reason": "stop_requested"}))
        recv_until(ws, lambda m: m["type"] == "session_stopped")


def test_bad_frames_are_rejected_then_connection_closed(secretary) -> None:  # type: ignore[no-untyped-def]
    _, _, token = _pair(secretary)
    with secretary.client.websocket_connect("/api/v1/ingest/ws", headers={"origin": EXT_ORIGIN}) as ws:
        ws.send_text(hello(token=token))
        ws.receive_json()
        for _ in range(3):
            ws.send_bytes(b"garbage-bytes-that-are-not-a-frame-at-all-000000000000")
            assert ws.receive_json()["code"] == "bad_frame"
        ws.send_bytes(b"x" * 10)
        msg = ws.receive_json()
        assert msg["fatal"] is True


def test_pairing_requires_allowed_extension_origin(secretary) -> None:  # type: ignore[no-untyped-def]
    meeting = create_meeting(secretary)
    confirm_consent(secretary, meeting["id"])
    cs = secretary.client.post(f"/api/v1/meetings/{meeting['id']}/capture-sessions", json={"mode": "companion"},
                               headers=secretary.headers()).json()
    code = cs["pairing"]["code"]
    other_ext = "chrome-extension://" + "p" * 32
    assert secretary.client.post("/api/v1/pairing/exchange", json={"code": code},
                                 headers={"origin": other_ext}).status_code == 403
    assert secretary.client.post("/api/v1/pairing/exchange", json={"code": code},
                                 headers={"origin": f"chrome-extension://{EXT_ID}"}).status_code == 200
    # one-time code
    assert secretary.client.post("/api/v1/pairing/exchange", json={"code": code},
                                 headers={"origin": f"chrome-extension://{EXT_ID}"}).status_code == 401
