"""Voice activity detection. Returned intervals are in samples of the audio passed in, so callers map them
back onto the original timeline by adding their own offset (VAD never shifts timestamps)."""

from __future__ import annotations

from typing import Protocol

import numpy as np

SR = 16000


class Vad(Protocol):
    name: str

    def speech_intervals(self, audio_16k: np.ndarray) -> list[tuple[int, int]]: ...


class SileroVad:
    """Silero VAD ONNX bundled with faster-whisper (local file, CPU)."""

    name = "silero"

    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 300, speech_pad_ms: int = 120) -> None:
        from faster_whisper.vad import VadOptions

        self._options = VadOptions(threshold=threshold, min_silence_duration_ms=min_silence_ms,
                                   speech_pad_ms=speech_pad_ms, min_speech_duration_ms=150)

    def speech_intervals(self, audio_16k: np.ndarray) -> list[tuple[int, int]]:
        from faster_whisper.vad import get_speech_timestamps

        if len(audio_16k) < SR // 10:
            return []
        return [(int(s["start"]), int(s["end"])) for s in get_speech_timestamps(audio_16k, self._options)]


class EnergyVad:
    """Frame-energy VAD with hysteresis. Deterministic; used in tests and for level diagnostics."""

    name = "energy"

    def __init__(self, threshold_dbfs: float = -45.0, frame_ms: int = 30, min_silence_ms: int = 300) -> None:
        self.threshold = 10 ** (threshold_dbfs / 20)
        self.frame = SR * frame_ms // 1000
        self.min_silence_frames = max(1, min_silence_ms // frame_ms)

    def speech_intervals(self, audio_16k: np.ndarray) -> list[tuple[int, int]]:
        n = len(audio_16k) // self.frame
        if n == 0:
            return []
        frames = audio_16k[: n * self.frame].reshape(n, self.frame)
        active = np.sqrt(np.mean(frames**2, axis=1)) > self.threshold
        intervals: list[tuple[int, int]] = []
        start = None
        silence = 0
        for i, is_active in enumerate(active):
            if is_active:
                if start is None:
                    start = i
                silence = 0
            elif start is not None:
                silence += 1
                if silence >= self.min_silence_frames:
                    intervals.append((start * self.frame, (i - silence + 1) * self.frame))
                    start, silence = None, 0
        if start is not None:
            intervals.append((start * self.frame, (n - silence) * self.frame))
        return intervals
