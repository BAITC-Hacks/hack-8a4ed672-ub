"""Windows-only: register DLL directories of the official NVIDIA pip wheels (nvidia-cublas-cu12).

CTranslate2's Windows wheel bundles cuDNN 9 but loads cuBLAS 12 (cublas64_12.dll) from the DLL
search path. Python >= 3.8 does not use PATH for dependent DLLs, so we add the wheel's `bin`
directory explicitly. No-op on other platforms (Linux images ship CUDA libs in the base image).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_registered: list[str] = []


def register_nvidia_dll_dirs() -> list[str]:
    if sys.platform != "win32" or _registered:
        return list(_registered)
    for pkg in ("nvidia.cublas", "nvidia.cuda_runtime", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ModuleNotFoundError, ValueError):
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        for location in spec.submodule_search_locations:
            bin_dir = Path(location) / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                _registered.append(str(bin_dir))
    return list(_registered)
