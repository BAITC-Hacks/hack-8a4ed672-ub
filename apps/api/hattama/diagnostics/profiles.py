"""Resource profile selection based on a HardwareReport.

The selector never silently falls back to a cloud; it only picks among local profiles and
explains why. Memory estimates are conservative planning numbers, replaced by measured
values in docs/model-selection.md once benchmarks are run.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hattama.diagnostics.hardware import HardwareReport

# Planning estimates (MiB). Measured values are recorded in reports/, not here.
EST_VRAM_MIB = {
    "whisper-large-v3-turbo-ct2:int8_float16": 1400,
    "whisper-large-v3-turbo-ct2:float16": 2000,
    "qwen3-8b-q4_k_m:full": 5900,  # ~5.0 GiB weights + KV(4096) + compute buffers
    "qwen3-4b-q4_k_m:full": 3300,
}
EST_RAM_MIB = {
    "live_worker": 1200,
    "api": 300,
    "pipeline_worker_idle": 400,
    "qwen3-8b-q4_k_m:weights": 5000,
    "qwen3-4b-q4_k_m:weights": 2500,
}
DESKTOP_VRAM_RESERVE_MIB = 400


@dataclass
class ProfileDecision:
    profile: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    llm_fit: dict[str, Any] = field(default_factory=dict)


def load_profiles(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("rb") as fh:
        return tomllib.load(fh)["profiles"]


def assess_llm_fit(report: HardwareReport, vram_free_mib: int) -> dict[str, Any]:
    """Explain whether Qwen3-8B Q4_K_M fits next to live ASR, and alternatives."""
    whisper = EST_VRAM_MIB["whisper-large-v3-turbo-ct2:int8_float16"]
    usable = max(0, vram_free_mib - DESKTOP_VRAM_RESERVE_MIB)
    ram_avail_mib = int(report.ram_available_gib * 1024)
    q8 = EST_VRAM_MIB["qwen3-8b-q4_k_m:full"]
    q4 = EST_VRAM_MIB["qwen3-4b-q4_k_m:full"]
    per_layer_8b = 5000 / 36
    layers_with_whisper = max(0, int((usable - whisper - 900) / per_layer_8b))  # 900: KV + buffers
    ram_needed_partial = int((36 - min(36, layers_with_whisper)) * per_layer_8b)
    return {
        "vram_usable_mib": usable,
        "ram_available_mib": ram_avail_mib,
        "qwen3_8b_full_offload_alone": q8 <= usable,
        "qwen3_8b_full_offload_with_live_asr": q8 + whisper <= usable,
        "qwen3_8b_layers_on_gpu_with_live_asr": min(36, layers_with_whisper),
        "qwen3_8b_ram_needed_with_live_asr_mib": ram_needed_partial,
        "qwen3_8b_partial_fits_ram_now": ram_needed_partial + EST_RAM_MIB["live_worker"]
        + EST_RAM_MIB["api"] <= ram_avail_mib,
        "qwen3_4b_full_offload_with_live_asr": q4 + whisper <= usable,
    }


def select_profile(report: HardwareReport) -> ProfileDecision:
    gpu = max(report.gpus, key=lambda g: g.memory_total_mib) if report.gpus else None
    if gpu is None:
        name = "cpu-8gb" if report.ram_total_gib < 12 else "cpu"
        decision = ProfileDecision(name, ["NVIDIA GPU не обнаружена — используется CPU-профиль"])
        if name == "cpu-8gb":
            decision.warnings.append("RAM < 12 GiB: LLM-анализ только после встречи; лучше подключиться как тонкий "
                                     "клиент к серверу с GPU (README → «Команда с 8 ГБ RAM»)")
        decision.warnings.append("CPU-профиль не обеспечивает real-time; очередь распознавания будет видна в интерфейсе")
        return decision
    cc = float(gpu.compute_capability) if gpu.compute_capability else 0.0
    if cc and cc < 6.0:
        return ProfileDecision("cpu", [f"GPU {gpu.name} (compute capability {cc}) не поддерживается CTranslate2 CUDA-сборкой"])
    if gpu.memory_total_mib >= 12000 and report.ram_total_gib >= 32:
        profile = "gpu-large"
    elif gpu.memory_total_mib >= 5500:
        profile = "gpu-6gb"
    else:
        return ProfileDecision("cpu", [f"VRAM {gpu.memory_total_mib} MiB недостаточно для live ASR на GPU"])
    decision = ProfileDecision(
        profile,
        [f"GPU {gpu.name}: {gpu.memory_total_mib} MiB VRAM (свободно {gpu.memory_free_mib} MiB), "
         f"драйвер {gpu.driver_version}, CUDA driver API {gpu.cuda_driver_version}",
         f"RAM: всего {report.ram_total_gib} GiB, доступно сейчас {report.ram_available_gib} GiB"],
    )
    decision.llm_fit = assess_llm_fit(report, gpu.memory_free_mib)
    if report.ram_available_gib < 4:
        decision.warnings.append(
            f"Доступно только {report.ram_available_gib} GiB RAM: закройте лишние приложения перед встречей; "
            "LLM-анализ будет выполняться после остановки live-распознавания"
        )
    if not decision.llm_fit.get("qwen3_8b_full_offload_with_live_asr"):
        decision.warnings.append(
            "Qwen3-8B Q4_K_M не помещается в VRAM вместе с live ASR: во время встречи LLM работает с частичной "
            "выгрузкой слоёв в RAM или откладывается до финального прохода"
        )
    return decision
