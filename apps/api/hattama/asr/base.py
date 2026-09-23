"""ASRProvider interface. Vendor calls live only in provider implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

import numpy as np

LanguageMode = Literal["auto", "ru", "kk", "mixed"]


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start: float  # seconds, relative to the audio passed to transcribe()
    end: float
    probability: float


@dataclass(slots=True)
class AsrSegment:
    text: str
    start: float
    end: float
    words: list[Word]
    language: str | None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


@dataclass(slots=True)
class AsrResult:
    segments: list[AsrSegment]
    language: str | None
    language_probability: float | None
    duration_s: float
    processing_s: float
    model_id: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecodeOptions:
    language_mode: LanguageMode = "mixed"
    beam_size: int = 1
    word_timestamps: bool = True
    initial_prompt: str | None = None
    hotwords: str | None = None
    vad_filter: bool = False
    temperature: tuple[float, ...] = (0.0, 0.2, 0.4)


class AsrUnavailableError(RuntimeError):
    """Model or runtime is unavailable (not prepared, OOM, missing CUDA libraries)."""


@runtime_checkable
class ASRProvider(Protocol):
    model_id: str
    device: str
    compute_type: str

    def transcribe(self, audio_16k: np.ndarray, options: DecodeOptions) -> AsrResult:
        """Transcribe mono float32 16 kHz audio. Must never translate (task=transcribe)."""
        ...

    def close(self) -> None: ...
