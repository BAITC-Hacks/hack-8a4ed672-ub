#!/usr/bin/env python3
"""Хаттама Live — единый раннер команд (Windows/Linux/macOS, только stdlib).

    python tasks.py <command> [options]

Команды: setup, models-prepare, runtimes-prepare, doctor, migrate, create-user, dev, llm, test,
test-integration, build-extension, smoke-local, contracts, lint.
Подготовка (скачивание) и офлайн-запуск — разные команды: dev/up/test никогда ничего не скачивают.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = sys.platform == "win32"
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
PYTHONPATH_DIRS = [ROOT / "apps" / "api", ROOT / "packages" / "contracts" / "python", ROOT / "apps" / "meeting-agent"]


def home_dir() -> Path:
    if IS_WIN and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Hattama"
    return Path(os.environ.get("HATTAMA_HOME", Path.home() / ".local" / "share" / "hattama"))


def env(extra: dict[str, str] | None = None) -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    # Python 3.11 decodes .pth files with the ANSI code page; explicit PYTHONPATH makes non-ASCII repo paths work.
    e["PYTHONPATH"] = os.pathsep.join(str(p) for p in PYTHONPATH_DIRS) + (
        os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.setdefault("HATTAMA_DATA_DIR", str(home_dir() / "data"))
    e.setdefault("HATTAMA_MODELS_DIR", str(home_dir() / "models"))
    e.setdefault("HATTAMA_TOOLS_DIR", str(home_dir() / "tools"))
    e.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if extra:
        e.update(extra)
    return e


def run(cmd: list[str], *, extra_env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> int:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    proc = subprocess.run([str(c) for c in cmd], env=env(extra_env), cwd=cwd or ROOT, check=False)
    if check and proc.returncode != 0:
        sys.exit(proc.returncode)
    return proc.returncode


def need_venv() -> None:
    if not PY.exists():
        sys.exit("Нет .venv — выполните: python tasks.py setup")


def npm() -> str:
    exe = shutil.which("npm.cmd" if IS_WIN else "npm") or shutil.which("npm")
    if not exe:
        sys.exit("npm не найден: установите Node.js 20+")
    return exe


def uv_cmd() -> list[str]:
    if shutil.which("uv"):
        return ["uv"]
    return [sys.executable, "-m", "uv"]


# ----------------------------------------------------------------------------- commands
def cmd_setup(a: argparse.Namespace) -> None:
    """Install locked dependencies (network needed once). Extras: --asr --gpu --diarization --agent."""
    extras = []
    if a.asr or a.gpu:
        extras += ["--extra", "asr"]
    if a.gpu and IS_WIN:
        extras += ["--extra", "cuda-win"]
    if a.diarization:
        extras += ["--extra", "diarization"]
    if a.agent:
        extras += ["--extra", "agent"]
    extras += ["--extra", "prepare"]
    run([*uv_cmd(), "sync", "--frozen", *extras])
    print("\nГотово. Далее: python tasks.py models-prepare && python tasks.py doctor")


def cmd_models_prepare(a: argparse.Namespace) -> None:
    need_venv()
    args = ["-m", "hattama.modelstore.prepare", "--manifest", str(ROOT / "model-configs" / "model-manifest.json"),
            "--models-dir", env()["HATTAMA_MODELS_DIR"]]
    if a.only:
        args += ["--only", *a.only]
    if a.list:
        args += ["--list"]
    run([PY, *args])


def cmd_runtimes_prepare(a: argparse.Namespace) -> None:
    need_venv()
    only = a.only or (["llama.cpp-win-cuda12.4"] if IS_WIN else [])
    if not only:
        sys.exit("На Linux llama.cpp запускается контейнером (deploy/compose.yaml, сервис llm)")
    run([PY, "-m", "hattama.modelstore.runtimes", "--manifest", str(ROOT / "model-configs" / "runtimes-manifest.json"),
         "--tools-dir", env()["HATTAMA_TOOLS_DIR"], "--only", *only])


def cmd_doctor(a: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "hattama.diagnostics.doctor", *(["--json"] if a.json else []),
         *(["--asr-test"] if a.asr_test else []), *(["--llm-test"] if a.llm_test else []),
         *(["--diarization-test"] if a.diarization_test else [])], check=False)


def cmd_migrate(_: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "hattama.cli", "migrate"])


def cmd_create_user(a: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "hattama.cli", "create-user", "--email", a.email, "--name", a.name, "--role", a.role])


def _llm_command() -> list[str] | None:
    sys.path[:0] = [str(p) for p in PYTHONPATH_DIRS]
    os.environ.update({k: v for k, v in env().items() if k.startswith("HATTAMA_")})
    from hattama.config import get_settings
    from hattama.modelstore.manifest import load_manifest, resolve_local_model
    from hattama.modelstore.runtimes import find_executable
    from hattama.runtime import active_profile

    settings = get_settings()
    cfg = active_profile(settings)["llm"]
    if cfg["model"] == "none":
        return None
    exe = find_executable(settings.tools_dir, "llama.cpp-win-cuda12.4" if IS_WIN else "llama.cpp-docker",
                          ROOT / "model-configs" / "runtimes-manifest.json") or shutil.which("llama-server")
    if exe is None:
        sys.exit("llama-server не найден: python tasks.py runtimes-prepare (Windows) или сервис llm в Docker")
    entry = load_manifest(settings.model_manifest)[cfg["model"]]
    model_dir = resolve_local_model(entry, settings.models_dir)
    gguf = next(model_dir.glob("*.gguf"))
    port = settings.llm_base_url.rsplit(":", 1)[-1].strip("/")
    return [str(exe), "-m", str(gguf), "--host", "127.0.0.1", "--port", port, "-c", str(cfg["ctx_size"]),
            "-np", str(cfg["parallel"]), "-ngl", str(cfg["n_gpu_layers"]), "--jinja", "--no-webui", "--metrics"]


def cmd_llm(_: argparse.Namespace) -> None:
    """Run the local llama.cpp server (127.0.0.1 only) with the profile's model."""
    need_venv()
    cmd = _llm_command()
    if cmd is None:
        sys.exit("В активном профиле LLM не задана")
    run(cmd)


def cmd_dev(a: argparse.Namespace) -> None:
    """Start API, ASR worker, pipeline worker, local LLM and the web dev server; Ctrl+C stops all."""
    need_venv()
    procs: list[tuple[str, subprocess.Popen]] = []

    def start(name: str, cmd: list[str], cwd: Path = ROOT) -> None:
        print(f"[dev] start {name}: {' '.join(map(str, cmd))}", flush=True)
        procs.append((name, subprocess.Popen([str(c) for c in cmd], env=env(), cwd=cwd)))

    run([PY, "-m", "hattama.cli", "migrate"])
    start("api", [PY, "-m", "hattama.cli", "api", "--port", str(a.port)])
    if not a.no_asr:
        start("asr-worker", [PY, "-m", "hattama.cli", "asr-worker"])
    start("pipeline-worker", [PY, "-m", "hattama.cli", "pipeline-worker"])
    if not a.no_llm:
        llm = _llm_command()
        if llm:
            start("llm", llm)
    print("\n[dev] Интерфейс: http://localhost:%d/  API: http://localhost:%d/api/docs  (Ctrl+C — остановить)\n"
          % (a.port, a.port), flush=True)
    try:
        while all(p.poll() is None for _, p in procs):
            time.sleep(1)
        for name, p in procs:
            if p.poll() is not None:
                print(f"[dev] процесс {name} завершился с кодом {p.returncode}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def cmd_test(a: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "pytest", "tests/unit", "-q", *a.pytest_args])
    if not a.no_node:
        run([shutil.which("node") or "node", "--test", "packages/audio-client/"])


def cmd_test_integration(a: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "pytest", "tests/integration", "-q", "-m", "not models", *a.pytest_args])


def cmd_build_extension(a: argparse.Namespace) -> None:
    args = [shutil.which("node") or "node", str(ROOT / "apps" / "extension" / "build.mjs")]
    if a.backend:
        args += ["--backend", a.backend]
    run(args)
    print("Chrome -> chrome://extensions -> Режим разработчика -> Загрузить распакованное -> apps/extension/dist")


def cmd_smoke_local(a: argparse.Namespace) -> None:
    """Full local route audio -> protocol with external egress blocked in-process (no network needed)."""
    need_venv()
    run([PY, "-m", "hattama.evaluation.smoke", *(["--with-models"] if a.with_models else [])],
        extra_env={"PYTHONPATH": os.pathsep.join([*map(str, PYTHONPATH_DIRS), str(ROOT / "tests")])})


def cmd_contracts(_: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "hattama.cli", "export-openapi"])
    run([PY, "-m", "hattama.cli", "export-schemas"])


def cmd_lint(_: argparse.Namespace) -> None:
    need_venv()
    run([PY, "-m", "ruff", "check", "apps/api", "packages/contracts/python", "apps/meeting-agent", "tests"])
    run([PY, "-m", "mypy"], check=False)


def main() -> None:
    p = argparse.ArgumentParser(prog="tasks.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--asr", action="store_true")
    s.add_argument("--gpu", action="store_true", help="ASR на NVIDIA GPU (Windows: cuBLAS из официального пакета NVIDIA)")
    s.add_argument("--diarization", action="store_true")
    s.add_argument("--agent", action="store_true")
    s.add_argument("--no-node", action="store_true")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("models-prepare")
    s.add_argument("--only", nargs="*")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_models_prepare)
    s = sub.add_parser("runtimes-prepare")
    s.add_argument("--only", nargs="*")
    s.set_defaults(fn=cmd_runtimes_prepare)
    s = sub.add_parser("doctor")
    s.add_argument("--json", action="store_true")
    s.add_argument("--asr-test", action="store_true")
    s.add_argument("--llm-test", action="store_true")
    s.add_argument("--diarization-test", action="store_true")
    s.set_defaults(fn=cmd_doctor)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    s = sub.add_parser("create-user")
    s.add_argument("--email", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--role", required=True, choices=["admin", "secretary", "manager", "assignee"])
    s.set_defaults(fn=cmd_create_user)
    s = sub.add_parser("dev")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--no-asr", action="store_true")
    s.set_defaults(fn=cmd_dev)
    s = sub.add_parser("llm")
    s.set_defaults(fn=cmd_llm)
    for name, fn in (("test", cmd_test), ("test-integration", cmd_test_integration)):
        s = sub.add_parser(name)
        s.add_argument("--no-node", action="store_true")
        s.add_argument("pytest_args", nargs="*")
        s.set_defaults(fn=fn)
    s = sub.add_parser("build-extension")
    s.add_argument("--backend", help="origin backend, например https://hattama.local:8443")
    s.set_defaults(fn=cmd_build_extension)
    s = sub.add_parser("smoke-local")
    s.add_argument("--with-models", action="store_true")
    s.set_defaults(fn=cmd_smoke_local)
    sub.add_parser("contracts").set_defaults(fn=cmd_contracts)
    sub.add_parser("lint").set_defaults(fn=cmd_lint)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
