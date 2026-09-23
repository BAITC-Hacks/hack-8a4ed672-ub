"""Build SYNTHETIC evaluation audio from fixtures/audio/synthetic/manifest.json (Windows only, offline).

Each item -> <id>.wav (16 kHz mono PCM16) + <id>.ref.json (reference text, speaker turns with times).
Turn times are TTS clip boundaries (include the voice's own leading/trailing silence), documented as such.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps" / "api"), str(ROOT / "packages" / "contracts" / "python")]
from hattama.audio.resample import StreamingResampler  # noqa: E402

FIX = ROOT / "fixtures" / "audio" / "synthetic"
SR = 16000


def tts(text: str, voice: str, tmp: Path) -> np.ndarray:
    txt = tmp / "t.txt"
    out = tmp / "t.wav"
    txt.write_text(text, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "scripts" / "synth_tts_onecore.ps1"), "-TextFile", str(txt), "-Out", str(out),
                    "-Voice", voice], check=True, capture_output=True)
    with wave.open(str(out), "rb") as wf:
        rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise RuntimeError("expected PCM16 from TTS")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    return StreamingResampler(rate, SR).process(audio, final=True) if rate != SR else audio


def main() -> int:
    if sys.platform != "win32":
        print("Генерация синтетического аудио требует Windows OneCore TTS", file=sys.stderr)
        return 2
    manifest = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for item in manifest["items"]:
            parts, turns, t = [], [], 0.0
            for turn in item["turns"]:
                clip = tts(turn["text"], turn["voice"], tmp)
                dur = len(clip) / SR
                turns.append({"speaker": turn["speaker"], "start_s": round(t, 3), "end_s": round(t + dur, 3),
                              "text": turn["text"], "voice": turn["voice"]})
                pause = np.zeros(int(turn.get("pause_after_s", 0.5) * SR), dtype=np.float32)
                parts += [clip, pause]
                t += dur + len(pause) / SR
            audio = np.concatenate(parts)
            pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
            with wave.open(str(FIX / f"{item['id']}.wav"), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SR)
                wf.writeframes(pcm)
            ref = {"id": item["id"], "synthetic": True, "generator": manifest["generator"],
                   "language": item["language"], "duration_s": round(len(audio) / SR, 3), "turns": turns,
                   "reference_text": " ".join(tu["text"] for tu in turns),
                   "turn_boundary_note": "границы реплик = границы TTS-клипов (включают собственную тишину голоса)"}
            (FIX / f"{item['id']}.ref.json").write_text(json.dumps(ref, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
            print(f"{item['id']}: {ref['duration_s']} s, {len(turns)} turns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
