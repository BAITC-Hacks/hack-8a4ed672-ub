"""Measure a local ASR configuration: load time, peak RAM/VRAM, RTF, output.

python -m hattama.diagnostics.asr_bench --model-dir DIR --device cuda --compute-type int8_float16 a.wav [b.wav ...]
Writes JSON to stdout; nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

from hattama.asr.base import AsrUnavailableError, DecodeOptions
from hattama.diagnostics.resources import ResourceSampler


def read_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: ожидается PCM16")
        rate, channels = wf.getframerate(), wf.getnchannels()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        from hattama.audio.resample import StreamingResampler

        data = StreamingResampler(rate, 16000).process(data, final=True)
    return data.astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--model-id", default="whisper-large-v3-turbo-ct2")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--language-mode", default="mixed", choices=["auto", "ru", "kk", "mixed"])
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--vad", action="store_true")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("wavs", nargs="+", type=Path)
    args = parser.parse_args(argv)

    from hattama.asr.faster_whisper_provider import FasterWhisperProvider

    report: dict = {"config": {k: str(v) for k, v in vars(args).items() if k != "wavs"}, "files": []}
    try:
        with ResourceSampler() as load_sampler:
            provider = FasterWhisperProvider(args.model_dir, model_id=args.model_id, device=args.device,
                                             compute_type=args.compute_type)
    except AsrUnavailableError as exc:
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1
    report["load"] = {"seconds": round(provider.load_seconds, 2), **load_sampler.peaks.to_dict()}
    options = DecodeOptions(language_mode=args.language_mode, beam_size=args.beam_size,
                            initial_prompt=args.prompt, vad_filter=args.vad)
    for wav in args.wavs:
        audio = read_wav_16k_mono(wav)
        provider.transcribe(audio[:16000], options)  # warm-up, excluded from timing
        with ResourceSampler() as sampler:
            started = time.perf_counter()
            result = provider.transcribe(audio, options)
            elapsed = time.perf_counter() - started
        report["files"].append({
            "file": wav.name,
            "duration_s": round(result.duration_s, 2),
            "processing_s": round(elapsed, 3),
            "rtf": round(elapsed / max(result.duration_s, 1e-6), 3),
            "language": result.language,
            "language_probability": result.language_probability,
            "segment_languages": [s.language for s in result.segments],
            "text": " ".join(s.text for s in result.segments),
            "resources": sampler.peaks.to_dict(),
        })
    provider.close()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
