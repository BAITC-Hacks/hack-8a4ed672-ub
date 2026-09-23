"""Sequential reader of a stored epoch: native-rate PCM -> 16 kHz float, with persistent resampler state.

Position bookkeeping is in native samples (`read_pos`) and 16 kHz samples (`out_pos`); the mapping
out_index -> native position is exact (StreamingResampler compensates filter delay).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from hattama.audio.resample import StreamingResampler
from hattama.ingest.store import read_pcm_range

SR = 16000


@dataclass
class EpochReader:
    pcm_path: Path
    sample_rate: int
    channel_count: int
    start_native: int = 0  # resume position (native samples)
    read_pos: int = field(init=False)
    out_pos: int = field(init=False)  # 16 kHz samples produced so far, measured from epoch start
    _resampler: StreamingResampler = field(init=False)

    def __post_init__(self) -> None:
        self.read_pos = self.start_native
        self.out_pos = int(round(self.start_native * SR / self.sample_rate))
        self._resampler = StreamingResampler(self.sample_rate, SR)
        self.finished = False

    def read_available(self, durable_samples: int, max_native: int | None = None, final: bool = False) -> np.ndarray:
        """Read [read_pos, durable_samples) (optionally capped). final=True flushes the resampler."""
        end = durable_samples if max_native is None else min(durable_samples, self.read_pos + max_native)
        if end <= self.read_pos and not final:
            return np.zeros(0, dtype=np.float32)
        chunk = read_pcm_range(self.pcm_path, self.channel_count, self.read_pos, max(end, self.read_pos))
        self.read_pos = max(end, self.read_pos)
        is_last = final and self.read_pos >= durable_samples
        out = self._resampler.process(chunk, final=is_last)
        if is_last:
            self.finished = True
        self.out_pos += len(out)
        return out

    def backlog_seconds(self, durable_samples: int) -> float:
        return max(0, durable_samples - self.read_pos) / self.sample_rate
