"""faster-whisper (CTranslate2) provider. Loads ONLY from a prepared local directory."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from hattama.asr.base import AsrResult, AsrSegment, AsrUnavailableError, DecodeOptions, Word
from hattama.asr.cuda_win import register_nvidia_dll_dirs

log = logging.getLogger(__name__)


def _language_args(mode: str) -> dict:
    # mixed: per-segment language detection; never force ru/kk for the whole meeting, never translate.
    if mode == "mixed":
        return {"language": None, "multilingual": True}
    if mode == "auto":
        return {"language": None, "multilingual": False}
    if mode in ("ru", "kk"):
        return {"language": mode, "multilingual": False}
    raise ValueError(f"unknown language mode {mode!r}")


class FasterWhisperProvider:
    def __init__(
        self,
        model_path: Path,
        *,
        model_id: str,
        device: str = "cuda",
        compute_type: str = "int8_float16",
        cpu_threads: int = 0,
    ) -> None:
        self.model_id = model_id
        self.device = device
        self.compute_type = compute_type
        self._lock = threading.Lock()
        if device == "cuda":
            register_nvidia_dll_dirs()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AsrUnavailableError("faster-whisper не установлен: python tasks.py setup --asr") from exc
        started = time.perf_counter()
        try:
            self._model = WhisperModel(
                str(model_path), device=device, compute_type=compute_type, cpu_threads=cpu_threads,
                num_workers=1, local_files_only=True,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            raise AsrUnavailableError(_explain_load_error(exc, device, compute_type)) from exc
        self.load_seconds = time.perf_counter() - started
        log.info("asr model loaded", extra={"model_id": model_id, "device": device,
                                            "compute_type": compute_type, "load_s": round(self.load_seconds, 2)})

    def transcribe(self, audio_16k: np.ndarray, options: DecodeOptions) -> AsrResult:
        if audio_16k.dtype != np.float32:
            audio_16k = audio_16k.astype(np.float32)
        params = {
            "task": "transcribe",
            "beam_size": options.beam_size,
            "word_timestamps": options.word_timestamps,
            "initial_prompt": options.initial_prompt,
            "hotwords": options.hotwords,
            "vad_filter": options.vad_filter,
            "condition_on_previous_text": False,
            "temperature": list(options.temperature),
            "hallucination_silence_threshold": 2.0 if options.word_timestamps else None,
            **_language_args(options.language_mode),
        }
        started = time.perf_counter()
        with self._lock:
            try:
                segments_iter, info = self._model.transcribe(audio_16k, **params)
                segments = [
                    AsrSegment(
                        text=s.text.strip(),
                        start=float(s.start),
                        end=float(s.end),
                        words=[Word(w.word, float(w.start), float(w.end), float(w.probability))
                               for w in (s.words or [])],
                        language=getattr(s, "language", None) or info.language,
                        avg_logprob=float(s.avg_logprob),
                        no_speech_prob=float(s.no_speech_prob),
                        compression_ratio=float(s.compression_ratio),
                    )
                    for s in segments_iter
                ]
            except RuntimeError as exc:
                raise AsrUnavailableError(_explain_load_error(exc, self.device, self.compute_type)) from exc
        elapsed = time.perf_counter() - started
        return AsrResult(
            segments=segments,
            language=info.language,
            language_probability=float(info.language_probability) if info.language_probability else None,
            duration_s=len(audio_16k) / 16000.0,
            processing_s=elapsed,
            model_id=self.model_id,
            params={k: v for k, v in params.items() if k not in ("initial_prompt", "hotwords")},
        )

    def close(self) -> None:
        model = getattr(self, "_model", None)
        if model is not None:
            del self._model


def _explain_load_error(exc: Exception, device: str, compute_type: str) -> str:
    text = str(exc)
    lowered = text.lower()
    if "out of memory" in lowered:
        return (f"Недостаточно памяти {device.upper()} для ASR ({compute_type}). Освободите VRAM/RAM или выберите "
                "профиль cpu в настройках. Переход на облако не выполняется.")
    if "cublas" in lowered or "cudnn" in lowered or ".dll" in lowered or "libcu" in lowered:
        return (f"Не найдены библиотеки CUDA для CTranslate2 ({text}). Windows: python tasks.py setup --gpu; "
                "Linux: используйте образ deploy/docker/Dockerfile.worker-gpu.")
    return f"ASR недоступен: {text}"
