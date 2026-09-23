"""Stateful streaming resampler (any rate -> 16 kHz) that keeps the timeline exact.

Chunks may arrive with arbitrary sizes; the output is identical (up to float rounding) to resampling
the concatenated stream at once. Anti-aliasing: linear-phase FIR low-pass whose group delay is
compensated, so output sample k maps to input position k * in_rate / out_rate.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import firwin, lfilter


class StreamingResampler:
    def __init__(self, in_rate: int, out_rate: int = 16000, numtaps: int | None = None) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("rates must be positive")
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.step = in_rate / out_rate  # input samples per output sample
        self.passthrough = in_rate == out_rate
        if in_rate > out_rate:
            taps = numtaps or (int(16 * self.step) | 1)
            self._taps = firwin(taps, cutoff=0.45 * out_rate, fs=in_rate).astype(np.float64)
        else:
            self._taps = np.array([1.0])
        self._delay = (len(self._taps) - 1) / 2.0
        self._zi = np.zeros(len(self._taps) - 1, dtype=np.float64)
        self._tail = np.zeros(0, dtype=np.float64)
        self._pos = self._delay  # position of next output sample in (tail + new) coordinates
        self.samples_in = 0
        self.samples_out = 0
        self._flushed = False

    def process(self, chunk: np.ndarray, final: bool = False) -> np.ndarray:
        if self._flushed:
            raise RuntimeError("resampler already flushed")
        x = np.asarray(chunk, dtype=np.float64).reshape(-1)
        self.samples_in += len(x)
        if self.passthrough:
            self.samples_out += len(x)
            if final:
                self._flushed = True
            return x.astype(np.float32)
        if final and self._delay > 0:
            x = np.concatenate([x, np.zeros(math.ceil(self._delay) + 1)])
        if len(self._taps) > 1:
            y, self._zi = lfilter(self._taps, 1.0, x, zi=self._zi)
        else:
            y = x
        buf = np.concatenate([self._tail, y])
        if final:
            # emit exactly round(samples_in / step) samples in total
            target_total = int(round(self.samples_in / self.step))
            available = max(0, target_total - self.samples_out)
        else:
            last = len(buf) - 1
            available = 0 if last <= self._pos else int(math.floor((last - self._pos) / self.step)) + 1
            # never read beyond buf[i0 + 1]
            while available > 0 and int(math.floor(self._pos + self.step * (available - 1))) + 1 > last:
                available -= 1
        if available == 0:
            out = np.zeros(0, dtype=np.float32)
        else:
            idx = self._pos + self.step * np.arange(available)
            i0 = np.floor(idx).astype(np.int64)
            frac = idx - i0
            i1 = np.minimum(i0 + 1, len(buf) - 1)
            i0 = np.minimum(i0, len(buf) - 1)
            out = (buf[i0] * (1.0 - frac) + buf[i1] * frac).astype(np.float32)
        next_pos = self._pos + self.step * available
        keep_from = min(int(math.floor(next_pos)), len(buf))
        self._tail = buf[keep_from:]
        self._pos = next_pos - keep_from
        self.samples_out += available
        if final:
            self._flushed = True
        return out

    def input_position_of_output(self, out_index: int) -> float:
        """Input-sample position (from stream start) of an output sample index."""
        return out_index * self.step


def pcm16_to_float_mono(data: bytes, channels: int) -> np.ndarray:
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples
