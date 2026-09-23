"""python tasks.py doctor [--json] [--asr-test] [--llm-test]

Checks: OS/runtime, RAM/VRAM, NVIDIA driver, CTranslate2 CUDA support, prepared models (receipts, sha256),
local LLM endpoint, synced-folder risk for data/models, recommended profile. Never downloads, never uses a cloud.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from hattama.config import get_settings
from hattama.diagnostics.hardware import probe
from hattama.diagnostics.profiles import select_profile
from hattama.modelstore.manifest import load_manifest, model_status


def collect(asr_test: bool = False, llm_test: bool = False) -> dict[str, Any]:
    settings = get_settings()
    hw = probe({"data_dir": settings.data_dir, "models_dir": settings.models_dir})
    decision = select_profile(hw)
    report: dict[str, Any] = {"hardware": hw.to_dict(), "recommended_profile": decision.__dict__,
                              "active_profile": settings.profile, "checks": []}

    def check(name: str, ok: bool | None, detail: str) -> None:
        report["checks"].append({"name": name, "status": "OK" if ok else ("SKIP" if ok is None else "FAIL"),
                                 "detail": detail})

    try:
        import ctranslate2

        check("ctranslate2", True, f"{ctranslate2.__version__}; CUDA devices: {ctranslate2.get_cuda_device_count()}")
    except ImportError:
        check("ctranslate2", False, "не установлен: python tasks.py setup --asr (или --gpu)")
    try:
        import torch

        check("torch", True, f"{torch.__version__}, cuda={torch.cuda.is_available()} (нужен только для диаризации)")
    except ImportError:
        check("torch", None, "не установлен — нужен только для диаризации (setup --diarization)")
    manifest = load_manifest(settings.model_manifest)
    for entry in manifest.values():
        st = model_status(entry, settings.models_dir)
        check(f"model:{entry.id}", st["prepared"] or (None if not entry.prepare_by_default else False),
              f"{st['path']} rev={st['revision']} sha256={list(st['verified_sha256'])}" if st["prepared"]
              else f"не подготовлена ({'gated, примите условия на HF' if entry.gated else 'models-prepare'})")
    for warn in hw.synced_folder_warnings:
        check("storage-sync", False, warn)
    from hattama.llm.client import LlamaCppClient, LlmUnavailableError

    try:
        client = LlamaCppClient(settings.llm_base_url, settings.llm_allowed_hosts, timeout_s=10)
        client.health()
        check("llm-endpoint", True, f"{client.base_url} model={client.model_name()}")
        if llm_test:
            from hattama.extraction.schemas import LlmEventsOut, llama_schema

            t = time.perf_counter()
            r = client.chat_json([{"role": "user", "content": "Ерлан, подготовьте отчёт до пятницы. Верни события."}],
                                 llama_schema(LlmEventsOut))
            check("llm-test", True, f"{time.perf_counter() - t:.1f} c, {r.completion_tokens} токенов")
    except (LlmUnavailableError, ValueError) as exc:
        check("llm-endpoint", False, f"{exc} — запустите: python tasks.py llm")
    if asr_test:
        from hattama.asr.base import AsrUnavailableError, DecodeOptions
        from hattama.runtime import make_asr_provider

        try:
            import numpy as np

            provider = make_asr_provider(settings, "live")
            t = time.perf_counter()
            provider.transcribe(np.zeros(16000 * 3, dtype=np.float32), DecodeOptions())
            check("asr-test", True, f"{provider.model_id} {provider.device}/{provider.compute_type}: загрузка "
                                    f"{getattr(provider, 'load_seconds', 0):.1f} c, 3 c тишины за "
                                    f"{time.perf_counter() - t:.2f} c")
        except AsrUnavailableError as exc:
            check("asr-test", False, str(exc))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--asr-test", action="store_true")
    parser.add_argument("--llm-test", action="store_true")
    parser.add_argument("--diarization-test", action="store_true")
    args = parser.parse_args(argv)
    report = collect(args.asr_test, args.llm_test)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    hw = report["hardware"]
    print(f"ОС: {hw['os_name']} {hw['os_release']} ({hw['runtime']}), Python {hw['python']}")
    print(f"CPU: {hw['cpu_model']} ({hw['cpu_logical_cores']} потоков); RAM {hw['ram_total_gib']} GiB, "
          f"доступно {hw['ram_available_gib']} GiB")
    for g in hw["gpus"]:
        print(f"GPU: {g['name']} {g['memory_total_mib']} MiB (свободно {g['memory_free_mib']}), драйвер "
              f"{g['driver_version']}, CUDA {g['cuda_driver_version']}")
    rec = report["recommended_profile"]
    print(f"Рекомендуемый профиль: {rec['profile']} (активный: {report['active_profile']})")
    for r in rec["reasons"] + rec["warnings"]:
        print("  -", r)
    for c in report["checks"]:
        print(f"[{c['status']:4}] {c['name']}: {c['detail']}")
    return 0 if all(c["status"] != "FAIL" for c in report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
