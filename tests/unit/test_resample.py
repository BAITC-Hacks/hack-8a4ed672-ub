from __future__ import annotations

import numpy as np
import pytest

from hattama.audio.resample import StreamingResampler


def tone(rate: int, seconds: float, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


@pytest.mark.parametrize("rate", [48000, 44100, 22050, 16000, 8000])
def test_chunked_equals_whole_and_length_exact(rate: int) -> None:
    x = tone(rate, 2.0)
    whole = StreamingResampler(rate).process(x, final=True)
    r = StreamingResampler(rate)
    rng = np.random.default_rng(0)
    parts, pos = [], 0
    while pos < len(x):
        n = int(rng.integers(1, 3000))
        parts.append(r.process(x[pos:pos + n], final=pos + n >= len(x)))
        pos += n
    chunked = np.concatenate(parts)
    assert len(whole) == len(chunked) == round(len(x) * 16000 / rate)
    assert np.max(np.abs(whole - chunked)) < 1e-5


def test_timeline_is_not_shifted_by_filter_delay() -> None:
    rate = 48000
    x = np.zeros(rate, dtype=np.float32)
    x[24000:24480] = 1.0  # 10 ms pulse at exactly 0.5 s
    y = StreamingResampler(rate).process(x, final=True)
    energy = y.astype(np.float64) ** 2
    centroid = float(np.sum(np.arange(len(y)) * energy) / np.sum(energy))
    assert abs(centroid / 16000 - 0.505) < 0.0005  # < 0.5 ms shift


def test_antialiasing_suppresses_above_nyquist() -> None:
    rate = 48000
    y = StreamingResampler(rate).process(tone(rate, 1.0, freq=12000), final=True)
    assert np.sqrt(np.mean(y[1000:-1000] ** 2)) < 0.01  # 12 kHz cannot exist at 16 kHz, must not alias
    y = StreamingResampler(rate).process(tone(rate, 1.0, freq=1000), final=True)
    assert np.sqrt(np.mean(y[1000:-1000] ** 2)) > 0.3
