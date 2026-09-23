"""Peak resource sampling (process RSS, GPU memory via nvidia-smi) during a measured block."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field


def gpu_memory_used_mib() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, check=False)
        return int(out.stdout.strip().splitlines()[0]) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


@dataclass
class ResourcePeaks:
    rss_start_mib: float = 0.0
    rss_peak_mib: float = 0.0
    gpu_start_mib: int | None = None
    gpu_peak_mib: int | None = None
    cpu_percent_avg: float | None = None
    samples: int = 0
    _cpu_samples: list[float] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {
            "rss_start_mib": round(self.rss_start_mib), "rss_peak_mib": round(self.rss_peak_mib),
            "gpu_start_mib": self.gpu_start_mib, "gpu_peak_mib": self.gpu_peak_mib,
            "gpu_delta_mib": (self.gpu_peak_mib - self.gpu_start_mib)
            if self.gpu_peak_mib is not None and self.gpu_start_mib is not None else None,
            "cpu_percent_avg": self.cpu_percent_avg, "samples": self.samples,
        }


class ResourceSampler:
    """Context manager: samples every `interval_s` in a background thread."""

    def __init__(self, interval_s: float = 0.25, pid: int | None = None) -> None:
        import psutil

        self._proc = psutil.Process(pid or os.getpid())
        self._interval = interval_s
        self._stop = threading.Event()
        self.peaks = ResourcePeaks()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        rss = self._proc.memory_info().rss / 2**20
        self.peaks.rss_peak_mib = max(self.peaks.rss_peak_mib, rss)
        gpu = gpu_memory_used_mib()
        if gpu is not None:
            self.peaks.gpu_peak_mib = max(self.peaks.gpu_peak_mib or 0, gpu)
        self.peaks._cpu_samples.append(self._proc.cpu_percent(interval=None))
        self.peaks.samples += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self._interval)

    def __enter__(self) -> ResourceSampler:
        self.peaks.rss_start_mib = self._proc.memory_info().rss / 2**20
        self.peaks.rss_peak_mib = self.peaks.rss_start_mib
        self.peaks.gpu_start_mib = gpu_memory_used_mib()
        self._proc.cpu_percent(interval=None)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sample()
        cpu = self.peaks._cpu_samples[1:] or self.peaks._cpu_samples
        self.peaks.cpu_percent_avg = round(sum(cpu) / len(cpu), 1) if cpu else None
        time.sleep(0)
