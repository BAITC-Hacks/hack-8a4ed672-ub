"""Position-addressed PCM storage for one capture epoch.

File layout per epoch: `<data>/captures/<capture_session>/<source_key>/epoch-<n>.pcm` (raw s16le interleaved,
native sample rate) and `epoch-<n>.idx` (fixed 28-byte records: sequence u64, start_sample u64,
capture_timestamp_us i64, sample_count u32) used as clock anchors and audit trail.
A frame is written at byte offset start_sample * channels * 2, so re-sending a frame is idempotent and
missing regions read back as zeros (silence) on the shared timeline.
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hattama_contracts.frame import AudioFrame

INDEX_RECORD = struct.Struct("<QQqI")


class EpochWriter:
    def __init__(self, pcm_path: Path, channel_count: int) -> None:
        pcm_path.parent.mkdir(parents=True, exist_ok=True)
        self.pcm_path = pcm_path
        self.idx_path = pcm_path.with_suffix(".idx")
        self.channel_count = channel_count
        self.bytes_per_sample_frame = 2 * channel_count
        mode = "r+b" if pcm_path.exists() else "w+b"
        self._pcm = open(pcm_path, mode)  # noqa: SIM115 - long-lived handle, closed in close()
        self._idx = open(self.idx_path, "ab")  # noqa: SIM115
        self._lock = threading.Lock()
        self._dirty = False
        self.closed = False

    def write(self, frame: AudioFrame) -> None:
        if frame.channel_count != self.channel_count:
            raise ValueError("channel_count mismatch for epoch")
        with self._lock:
            self._pcm.seek(frame.start_sample * self.bytes_per_sample_frame)
            self._pcm.write(frame.payload)
            self._idx.write(INDEX_RECORD.pack(frame.sequence, frame.start_sample, frame.capture_timestamp_us,
                                              frame.sample_count))
            self._dirty = True

    def sync(self) -> None:
        """flush + fsync: after this returns, written frames survive a process/OS crash."""
        with self._lock:
            if not self._dirty or self.closed:
                return
            self._pcm.flush()
            os.fsync(self._pcm.fileno())
            self._idx.flush()
            os.fsync(self._idx.fileno())
            self._dirty = False

    def close(self) -> None:
        if self.closed:
            return
        self.sync()
        with self._lock:
            self._pcm.close()
            self._idx.close()
            self.closed = True


def read_pcm_range(pcm_path: Path, channel_count: int, start_sample: int, end_sample: int) -> np.ndarray:
    """Read samples [start, end) as float32 mono. Missing tail (not yet written) is zero-padded."""
    count = max(0, end_sample - start_sample)
    bpf = 2 * channel_count
    out = np.zeros(count, dtype=np.float32)
    if count == 0 or not pcm_path.exists():
        return out
    with open(pcm_path, "rb") as fh:
        fh.seek(start_sample * bpf)
        raw = fh.read(count * bpf)
    usable = len(raw) // bpf
    if usable:
        samples = np.frombuffer(raw[: usable * bpf], dtype="<i2").astype(np.float32) / 32768.0
        if channel_count > 1:
            samples = samples.reshape(-1, channel_count).mean(axis=1)
        out[:usable] = samples
    return out


def read_index(idx_path: Path) -> list[tuple[int, int, int, int]]:
    if not idx_path.exists():
        return []
    data = idx_path.read_bytes()
    n = len(data) // INDEX_RECORD.size
    return [INDEX_RECORD.unpack_from(data, i * INDEX_RECORD.size) for i in range(n)]


@dataclass
class GapDecision:
    start_sample: int
    end_sample: int
    from_sequence: int
    to_sequence: int
    reason: str


@dataclass
class _Pending:
    start_sample: int
    end_sample: int
    arrived_at: float
    lost: bool = False
    last_sequence: int = -1  # for lost ranges


class SequenceTracker:
    """Tracks contiguity of one epoch's frames; decides ACK watermark and gaps. Pure logic, no I/O."""

    def __init__(self, contiguous_seq: int = -1, contiguous_end: int = 0, reorder_window: int = 64,
                 reorder_timeout_s: float = 2.0) -> None:
        self.contiguous_seq = contiguous_seq
        self.contiguous_end = contiguous_end
        self.reorder_window = reorder_window
        self.reorder_timeout_s = reorder_timeout_s
        self._pending: dict[int, _Pending] = {}
        self.duplicates = 0
        self.received = 0
        self.gap_ranges: list[tuple[int, int, int, int]] = []  # (from_seq, to_seq, start_sample, end_sample)

    def is_duplicate(self, seq: int) -> bool:
        return seq <= self.contiguous_seq or seq in self._pending

    def in_declared_gap(self, seq: int) -> tuple[int, int, int, int] | None:
        for rng in self.gap_ranges:
            if rng[0] <= seq <= rng[1]:
                return rng
        return None

    def accept(self, seq: int, start_sample: int, sample_count: int, now: float) -> list[GapDecision]:
        """Register a written frame (caller already wrote it). Returns gaps declared by timeout/window."""
        self.received += 1
        if self.is_duplicate(seq):
            self.duplicates += 1
            return []
        self._pending[seq] = _Pending(start_sample, start_sample + sample_count, now)
        self._absorb()
        return self._expire(now)

    def report_lost(self, from_seq: int, to_seq: int, start_sample: int, lost_samples: int,
                    now: float) -> list[GapDecision]:
        """Client declared frames [from_seq, to_seq] lost (e.g. buffer overflow)."""
        if to_seq <= self.contiguous_seq:
            return []
        if from_seq <= self.contiguous_seq:
            # part of the reported range is already stored: keep only the tail after the watermark
            end = start_sample + lost_samples
            from_seq, start_sample = self.contiguous_seq + 1, max(start_sample, self.contiguous_end)
            lost_samples = max(0, end - start_sample)
        self._pending[from_seq] = _Pending(start_sample, start_sample + lost_samples, now, lost=True,
                                           last_sequence=to_seq)
        decision = GapDecision(start_sample, start_sample + lost_samples, from_seq, to_seq, "client_buffer_overflow")
        self.gap_ranges.append((from_seq, to_seq, start_sample, start_sample + lost_samples))
        self._absorb()
        return [decision, *self._expire(now)]

    def _absorb(self) -> None:
        while True:
            nxt = self.contiguous_seq + 1
            item = self._pending.pop(nxt, None)
            if item is None:
                return
            self.contiguous_seq = item.last_sequence if item.lost else nxt
            self.contiguous_end = max(self.contiguous_end, item.end_sample)

    def _expire(self, now: float) -> list[GapDecision]:
        decisions: list[GapDecision] = []
        while self._pending:
            first_seq = min(self._pending)
            oldest = min(p.arrived_at for p in self._pending.values())
            if len(self._pending) <= self.reorder_window and now - oldest < self.reorder_timeout_s:
                break
            first = self._pending[first_seq]
            gap = GapDecision(self.contiguous_end, first.start_sample, self.contiguous_seq + 1, first_seq - 1,
                              "sequence_gap")
            if gap.end_sample > gap.start_sample or gap.to_sequence >= gap.from_sequence:
                decisions.append(gap)
                self.gap_ranges.append((gap.from_sequence, gap.to_sequence, gap.start_sample, gap.end_sample))
            self.contiguous_seq = first_seq - 1
            self._absorb()
        return decisions

    def flush_timeouts(self, now: float) -> list[GapDecision]:
        return self._expire(now)

    @property
    def pending_count(self) -> int:
        return len(self._pending)
