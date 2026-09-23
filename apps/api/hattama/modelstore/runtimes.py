"""Native runtime preparation (llama.cpp server binaries) with sha256 verification.

Only used by `tasks.py runtimes-prepare`; the product never downloads at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    if not url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/"):
        raise SystemExit(f"URL вне разрешённого источника: {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:  # noqa: S310 - pinned https URL
        shutil.copyfileobj(resp, out, length=4 * 1024 * 1024)
    tmp.replace(dest)


def prepare_runtime(entry: dict, tools_dir: Path) -> Path:
    target = tools_dir / entry["local_dir"]
    receipt = target / ".hattama-runtime.json"
    if receipt.is_file() and (target / entry["executable"]).is_file():
        print(f"[runtimes-prepare] {entry['id']} уже подготовлен: {target}")
        return target
    target.mkdir(parents=True, exist_ok=True)
    downloads = tools_dir / "_downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    verified = {}
    for asset in entry["assets"]:
        name = asset["url"].rsplit("/", 1)[-1]
        archive = downloads / name
        if not archive.is_file() or _sha256(archive) != asset["sha256"]:
            print(f"  -> {asset['url']}", flush=True)
            _download(asset["url"], archive)
        actual = _sha256(archive)
        if actual != asset["sha256"]:
            archive.unlink(missing_ok=True)
            raise SystemExit(f"sha256 {name} не совпадает с манифестом — архив удалён")
        verified[name] = actual
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                resolved = (target / member).resolve()
                if not str(resolved).startswith(str(target.resolve())):
                    raise SystemExit(f"Небезопасный путь в архиве: {member}")
            zf.extractall(target)
    exe = next(target.rglob(entry["executable"]), None)
    if exe is None:
        raise SystemExit(f"{entry['executable']} не найден после распаковки")
    receipt.write_text(
        json.dumps({"id": entry["id"], "version": entry["version"], "verified_sha256": verified,
                    "executable": str(exe.relative_to(target))}, indent=2),
        encoding="utf-8",
    )
    print(f"[runtimes-prepare] OK {entry['id']}: {exe}")
    return target


def find_executable(tools_dir: Path, runtime_id: str, manifest_path: Path) -> Path | None:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in data["runtimes"]:
        if entry["id"] == runtime_id and "local_dir" in entry:
            receipt = tools_dir / entry["local_dir"] / ".hattama-runtime.json"
            if receipt.is_file():
                rel = json.loads(receipt.read_text(encoding="utf-8"))["executable"]
                exe = tools_dir / entry["local_dir"] / rel
                return exe if exe.is_file() else None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtimes-prepare")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tools-dir", type=Path, required=True)
    parser.add_argument("--only", nargs="+", required=True)
    args = parser.parse_args(argv)
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in data["runtimes"]}
    for rid in args.only:
        entry = by_id.get(rid)
        if entry is None or "assets" not in entry:
            print(f"Неизвестный или не скачиваемый runtime: {rid}", file=sys.stderr)
            return 2
        prepare_runtime(entry, args.tools_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
