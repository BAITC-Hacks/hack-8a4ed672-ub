"""Binary audio frame codec for protocol `hattama.audio.v1` (see packages/contracts/audio-frame-v1.md)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"HTA1"
VERSION = 1
KIND_PCM_S16LE = 1
HEADER = struct.Struct("<4sBBHHHIQIIqQ")
HEADER_LENGTH = HEADER.size  # 48
BYTES_PER_SAMPLE = 2
ALLOWED_SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000})
MAX_CHANNELS = 2
DEFAULT_MAX_FRAME_BYTES = 512 * 1024

assert HEADER_LENGTH == 48, HEADER_LENGTH


class FrameError(ValueError):
    """Frame violates the protocol. `code` is stable and sent to the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AudioFrame:
    source_index: int
    channel_count: int
    capture_epoch: int
    sequence: int
    sample_rate: int
    sample_count: int
    capture_timestamp_us: int
    start_sample: int
    payload: bytes

    @property
    def end_sample(self) -> int:
        return self.start_sample + self.sample_count


def encode_frame(frame: AudioFrame) -> bytes:
    expected = frame.sample_count * frame.channel_count * BYTES_PER_SAMPLE
    if len(frame.payload) != expected:
        raise FrameError("bad_payload_length", f"payload {len(frame.payload)} != {expected}")
    header = HEADER.pack(
        MAGIC, VERSION, KIND_PCM_S16LE, HEADER_LENGTH,
        frame.source_index, frame.channel_count, frame.capture_epoch, frame.sequence,
        frame.sample_rate, frame.sample_count, frame.capture_timestamp_us, frame.start_sample,
    )
    return header + frame.payload


def decode_frame(data: bytes, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> AudioFrame:
    if len(data) > max_frame_bytes:
        raise FrameError("frame_too_large", f"frame {len(data)} bytes > limit {max_frame_bytes}")
    if len(data) < HEADER_LENGTH:
        raise FrameError("short_frame", f"frame {len(data)} bytes < header {HEADER_LENGTH}")
    (magic, version, kind, header_length, source_index, channel_count, capture_epoch, sequence,
     sample_rate, sample_count, capture_ts, start_sample) = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise FrameError("bad_magic", "magic mismatch")
    if version != VERSION:
        raise FrameError("unsupported_version", f"version {version}")
    if kind != KIND_PCM_S16LE:
        raise FrameError("unsupported_kind", f"kind {kind}")
    if header_length != HEADER_LENGTH:
        raise FrameError("bad_header_length", f"header_length {header_length}")
    if not 1 <= channel_count <= MAX_CHANNELS:
        raise FrameError("bad_channel_count", f"channel_count {channel_count}")
    if sample_rate not in ALLOWED_SAMPLE_RATES:
        raise FrameError("bad_sample_rate", f"sample_rate {sample_rate}")
    if not 1 <= sample_count <= sample_rate * 2:
        raise FrameError("bad_sample_count", f"sample_count {sample_count}")
    payload = data[HEADER_LENGTH:]
    expected = sample_count * channel_count * BYTES_PER_SAMPLE
    if len(payload) != expected:
        raise FrameError("bad_payload_length", f"payload {len(payload)} != {expected}")
    return AudioFrame(
        source_index=source_index, channel_count=channel_count, capture_epoch=capture_epoch,
        sequence=sequence, sample_rate=sample_rate, sample_count=sample_count,
        capture_timestamp_us=capture_ts, start_sample=start_sample, payload=bytes(payload),
    )
