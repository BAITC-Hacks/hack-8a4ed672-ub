from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from hattama_contracts.frame import HEADER_LENGTH, AudioFrame, FrameError, decode_frame, encode_frame
from hattama_contracts.messages import client_message_adapter

VECTORS = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "test-vectors" / "audio-frame-v1.json"


def _frame(**kw) -> AudioFrame:  # type: ignore[no-untyped-def]
    base = dict(source_index=1, channel_count=1, capture_epoch=2, sequence=7, sample_rate=48000, sample_count=4,
                capture_timestamp_us=1_700_000_000_000_000, start_sample=28, payload=struct.pack("<4h", 0, 1, -1, 32767))
    base.update(kw)
    return AudioFrame(**base)


def test_header_is_48_bytes_little_endian() -> None:
    data = encode_frame(_frame())
    assert HEADER_LENGTH == 48
    assert data[:4] == b"HTA1"
    assert data[4] == 1 and data[5] == 1
    assert struct.unpack_from("<H", data, 6)[0] == 48
    assert struct.unpack_from("<Q", data, 16)[0] == 7
    assert struct.unpack_from("<Q", data, 40)[0] == 28
    assert len(data) == 48 + 8


def test_roundtrip() -> None:
    frame = _frame()
    assert decode_frame(encode_frame(frame)) == frame


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda b: b"XXXX" + b[4:], "bad_magic"),
        (lambda b: b[:4] + bytes([2]) + b[5:], "unsupported_version"),
        (lambda b: b[:5] + bytes([9]) + b[6:], "unsupported_kind"),
        (lambda b: b[:-2], "bad_payload_length"),
        (lambda b: b[:20], "short_frame"),
    ],
)
def test_rejects_malformed(mutate, code) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(FrameError) as err:
        decode_frame(mutate(encode_frame(_frame())))
    assert err.value.code == code


def test_rejects_bad_rate_channels_and_size() -> None:
    raw = bytearray(encode_frame(_frame()))
    struct.pack_into("<I", raw, 24, 12345)
    with pytest.raises(FrameError, match="sample_rate"):
        decode_frame(bytes(raw))
    raw = bytearray(encode_frame(_frame()))
    struct.pack_into("<H", raw, 10, 3)
    with pytest.raises(FrameError) as err:
        decode_frame(bytes(raw))
    assert err.value.code == "bad_channel_count"
    big = _frame(sample_count=48000, payload=b"\0" * 96000)
    with pytest.raises(FrameError) as err:
        decode_frame(encode_frame(big), max_frame_bytes=64 * 1024)
    assert err.value.code == "frame_too_large"


def test_shared_test_vectors_match_python_codec() -> None:
    """Same vectors are verified by the TypeScript codec (packages/audio-client tests)."""
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    for vec in vectors["vectors"]:
        fields = {k: v for k, v in vec["frame"].items() if k != "payload_hex"}
        frame = AudioFrame(**fields, payload=bytes.fromhex(vec["frame"]["payload_hex"]))
        assert encode_frame(frame).hex() == vec["hex"], vec["name"]
        assert decode_frame(bytes.fromhex(vec["hex"])) == frame


def test_client_messages_reject_unknown_fields_and_bad_ids() -> None:
    ok = {"type": "source_open", "source_id": "tab", "kind": "tab_audio", "sample_rate": 48000, "channel_count": 1,
          "capture_epoch": 0, "epoch_start_wall_us": 1}
    client_message_adapter.validate_python(ok)
    with pytest.raises(ValueError):
        client_message_adapter.validate_python({**ok, "evil": 1})
    with pytest.raises(ValueError):
        client_message_adapter.validate_python({**ok, "source_id": "../etc"})
    with pytest.raises(ValueError):
        client_message_adapter.validate_python({"type": "hello", "protocol": "other", "client": {"name": "x",
                                                                                                 "version": "1"}})
