"""Проверка без Chrome: отправляет WAV-файл на сервер ТАК ЖЕ, как расширение (протокол hattama.audio.v1).

    python scripts/stream_wav.py --code ABCD-EFGH [--server http://localhost:8000] [--wav fixtures/audio/synthetic/ru_synth_dialog_b.wav] [--speed 1]

Код сопряжения берётся на странице встречи: «Расширение Chrome: получить код сопряжения».
--speed 1 = реальное время (как на живой встрече), 2 = вдвое быстрее. После отправки сессия останавливается,
и сервер запускает финальную обработку.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "contracts" / "python"))
from hattama_contracts.frame import AudioFrame, encode_frame  # noqa: E402

EXT_ORIGIN = "chrome-extension://bibicplbpcephoocaemgdjjbbhfhfion"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--wav", default=str(ROOT / "fixtures" / "audio" / "synthetic" / "ru_synth_dialog_b.wav"))
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    r = httpx.post(f"{a.server}/api/v1/pairing/exchange", json={"code": a.code, "client_name": "stream_wav.py"},
                   headers={"origin": EXT_ORIGIN}, timeout=10)
    if r.status_code != 200:
        print("Сопряжение не удалось:", r.status_code, r.text)
        return 1
    pairing = r.json()
    print(f"Сопряжено: встреча «{pairing['meeting_title']}»")
    with wave.open(a.wav, "rb") as wf:
        rate, ch = wf.getframerate(), wf.getnchannels()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype("<i2")
    chunk = rate // 5  # 200 ms, as the extension does
    ws_url = a.server.replace("http", "ws", 1) + "/api/v1/ingest/ws"
    async with websockets.connect(ws_url, origin=EXT_ORIGIN, max_size=2**20) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol": "hattama.audio.v1", "token": pairing["token"],
                                  "client": {"name": "stream_wav.py", "version": "1", "platform": sys.platform}}))
        print("Сервер:", json.loads(await ws.recv())["type"])
        start_us = int(time.time() * 1e6)
        await ws.send(json.dumps({"type": "source_open", "source_id": "tab", "kind": "tab_audio", "sample_rate": rate,
                                  "channel_count": 1, "capture_epoch": 0, "epoch_start_wall_us": start_us,
                                  "label": "WAV-файл (эмуляция вкладки)"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "source_ready":
                idx = msg["source_index"]
                break
        acked = -1

        async def reader() -> None:
            nonlocal acked
            async for raw in ws:
                m = json.loads(raw)
                if m["type"] == "ack":
                    acked = m["durable_sequence"]
                elif m["type"] in ("error", "gap_recorded", "session_stopped", "stop_requested"):
                    print("Сервер:", m)
                    if m["type"] == "session_stopped":
                        return

        task = asyncio.create_task(reader())
        seq = 0
        t0 = time.time()
        for pos in range(0, len(pcm), chunk):
            part = pcm[pos:pos + chunk]
            await ws.send(encode_frame(AudioFrame(idx, 1, 0, seq, rate, len(part), start_us + int(pos / rate * 1e6),
                                                  pos, part.tobytes())))
            seq += 1
            target = t0 + (pos + len(part)) / rate / a.speed
            await asyncio.sleep(max(0.0, target - time.time()))
            if seq % 25 == 0:
                print(f"  отправлено {pos / rate:5.1f} c, подтверждено сервером до кадра {acked}")
        while acked < seq - 1:
            await asyncio.sleep(0.1)
        print(f"Все {seq} кадров подтверждены (fsync). Останавливаю запись…")
        await ws.send(json.dumps({"type": "stop_session", "reason": "user_stop"}))
        await ws.send(json.dumps({"type": "source_close", "source_index": idx, "capture_epoch": 0,
                                  "final_sequence": seq - 1, "reason": "stop_requested"}))
        await asyncio.wait_for(task, timeout=30)
    print("Готово: откройте встречу в веб-панели — после финальной обработки статус станет NEEDS_REVIEW.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
