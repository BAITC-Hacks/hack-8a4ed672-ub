"""Test-side protocol client for hattama.audio.v1 over Starlette's TestClient WebSocket."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from hattama_contracts.frame import AudioFrame, encode_frame

EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def hello(token: str | None = None, capture_session_id: str | None = None) -> str:
    return json.dumps({"type": "hello", "protocol": "hattama.audio.v1", "token": token,
                       "capture_session_id": capture_session_id,
                       "client": {"name": "pytest", "version": "1", "platform": "test"}})


def source_open(source_id: str = "tab", kind: str = "tab_audio", rate: int = 48000, epoch: int = 0,
                start_us: int | None = None, channels: int = 1) -> str:
    return json.dumps({"type": "source_open", "source_id": source_id, "kind": kind, "sample_rate": rate,
                       "channel_count": channels, "capture_epoch": epoch,
                       "epoch_start_wall_us": start_us or int(time.time() * 1e6)})


def pcm_frame(index: int, seq: int, start: int, samples: np.ndarray, rate: int = 48000, epoch: int = 0,
              start_us: int = 0) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    return encode_frame(AudioFrame(index, 1, epoch, seq, rate, len(samples),
                                   start_us + int(start / rate * 1e6), start, pcm))


def recv_until(ws: Any, pred: Callable[[dict], bool], limit: int = 200) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
        if msg.get("type") == "error" and msg.get("fatal"):
            raise AssertionError(f"fatal error: {msg}")
    raise AssertionError("expected message not received")


def wait_ack(ws: Any, seq: int) -> dict:
    return recv_until(ws, lambda m: m.get("type") == "ack" and m["durable_sequence"] >= seq)
