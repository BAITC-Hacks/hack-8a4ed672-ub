"""Hardware and runtime environment probe.

Pure diagnostics: no model loading, no network access. Used by `tasks.py doctor`,
the settings/diagnostics API and the resource-profile selector.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

GIB = 1024**3


@dataclass
class GpuInfo:
    name: str
    memory_total_mib: int
    memory_used_mib: int
    memory_free_mib: int
    driver_version: str
    compute_capability: str | None
    cuda_driver_version: str | None


@dataclass
class HardwareReport:
    os_name: str
    os_release: str
    runtime: str  # windows | wsl | linux | macos | other
    python: str
    cpu_model: str
    cpu_logical_cores: int
    ram_total_gib: float
    ram_available_gib: float
    gpus: list[GpuInfo] = field(default_factory=list)
    nvidia_driver_present: bool = False
    disk_free_gib: dict[str, float] = field(default_factory=dict)
    synced_folder_warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def detect_runtime() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        try:
            version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            version = ""
        if "microsoft" in version or "wsl" in version:
            return "wsl"
        return "linux"
    return "other"


def _cpu_model() -> str:
    if platform.system() == "Windows":
        name = platform.processor()
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                name = out.stdout.strip().splitlines()[0]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return name or "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _memory() -> tuple[float, float]:
    try:
        import psutil

        vm = psutil.virtual_memory()
        return vm.total / GIB, vm.available / GIB
    except ImportError:
        pass
    if hasattr(os, "sysconf"):
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            avail = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
            return total / GIB, avail / GIB
        except (ValueError, OSError):
            pass
    return 0.0, 0.0


def probe_nvidia() -> tuple[bool, list[GpuInfo], str | None]:
    """Query nvidia-smi. Returns (driver_present, gpus, error)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False, [], "nvidia-smi не найден (драйвер NVIDIA не установлен или GPU отсутствует)"
    query = "name,memory.total,memory.used,memory.free,driver_version,compute_cap"
    try:
        out = subprocess.run(
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return True, [], f"nvidia-smi не ответил: {exc}"
    if out.returncode != 0:
        # older drivers may not support compute_cap
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if out.returncode != 0:
            return True, [], f"nvidia-smi завершился с кодом {out.returncode}"
    cuda_version = None
    try:
        banner = subprocess.run([exe], capture_output=True, text=True, timeout=20, check=False).stdout
        for token in banner.split("|"):
            if "CUDA Version" in token:
                cuda_version = token.split("CUDA Version:")[1].strip().split()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    gpus: list[GpuInfo] = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append(
            GpuInfo(
                name=parts[0],
                memory_total_mib=int(float(parts[1])),
                memory_used_mib=int(float(parts[2])),
                memory_free_mib=int(float(parts[3])),
                driver_version=parts[4],
                compute_capability=parts[5] if len(parts) > 5 else None,
                cuda_driver_version=cuda_version,
            )
        )
    return True, gpus, None


SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "yandex.disk", "yandexdisk")


def synced_folder_warning(path: Path) -> str | None:
    """Detect cloud-synced folders: storing meeting data there would upload it to a cloud."""
    lowered = str(path.resolve()).lower()
    for marker in SYNC_MARKERS:
        if marker in lowered:
            return (
                f"Путь {path} похож на папку облачной синхронизации ({marker}). "
                "Записи встреч, транскрипты и веса моделей там хранить нельзя: они будут выгружены в облако."
            )
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(env)
        if root and lowered.startswith(str(Path(root).resolve()).lower()):
            return f"Путь {path} находится внутри OneDrive ({root})."
    return None


def probe(paths: dict[str, Path] | None = None) -> HardwareReport:
    total, available = _memory()
    driver_present, gpus, gpu_error = probe_nvidia()
    report = HardwareReport(
        os_name=platform.system(),
        os_release=f"{platform.release()} ({platform.version()})",
        runtime=detect_runtime(),
        python=sys.version.split()[0],
        cpu_model=_cpu_model(),
        cpu_logical_cores=os.cpu_count() or 0,
        ram_total_gib=round(total, 1),
        ram_available_gib=round(available, 1),
        gpus=gpus,
        nvidia_driver_present=driver_present and bool(gpus),
    )
    if gpu_error:
        report.notes.append(gpu_error)
    for label, path in (paths or {}).items():
        target = path
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            report.disk_free_gib[label] = round(shutil.disk_usage(target).free / GIB, 1)
        except OSError as exc:
            report.notes.append(f"{label}: не удалось определить свободное место ({exc})")
        warning = synced_folder_warning(path)
        if warning:
            report.synced_folder_warnings.append(f"{label}: {warning}")
    return report
