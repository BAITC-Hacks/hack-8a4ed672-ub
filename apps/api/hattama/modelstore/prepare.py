"""Model preparation (the ONLY code path that downloads weights).

Usage: python tasks.py models-prepare [--only ID ...] [--models-dir DIR]
Token: huggingface_hub reads the local HF login or HF_TOKEN; it is never printed or stored here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from hattama.modelstore.manifest import RECEIPT_NAME, ModelEntry, load_manifest, sha256_file


def _download_entry(entry: ModelEntry, models_dir: Path) -> dict:
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
        from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
    except ImportError as exc:  # pragma: no cover - environment issue
        raise SystemExit("huggingface_hub не установлен: выполните python tasks.py setup") from exc

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    target = models_dir / entry.local_dir
    target.mkdir(parents=True, exist_ok=True)
    if entry.revision is None:
        raise SystemExit(f"{entry.id}: в манифесте не закреплена revision — подготовка запрещена")
    started = time.time()
    verified: dict[str, str] = {}
    try:
        if entry.files == "*":
            snapshot_download(repo_id=entry.repo_id, revision=entry.revision, local_dir=target)
        else:
            assert isinstance(entry.files, list)
            for spec in entry.files:
                print(f"  -> {entry.repo_id}@{entry.revision[:10]} / {spec['path']}", flush=True)
                hf_hub_download(
                    repo_id=entry.repo_id, filename=spec["path"], revision=entry.revision, local_dir=target
                )
    except GatedRepoError as exc:
        raise SystemExit(
            f"{entry.id}: доступ закрыт. Примите условия модели на https://huggingface.co/{entry.repo_id} "
            "под своим аккаунтом и повторите (токен берётся из локального `hf auth login` или HF_TOKEN)."
        ) from exc
    except RepositoryNotFoundError as exc:
        raise SystemExit(f"{entry.id}: репозиторий {entry.repo_id} недоступен ({type(exc).__name__})") from exc

    if isinstance(entry.files, list):
        for spec in entry.files:
            path = target / spec["path"]
            if "size" in spec and path.stat().st_size != spec["size"]:
                raise SystemExit(f"{entry.id}: размер {spec['path']} не совпадает с манифестом")
            if "sha256" in spec:
                actual = sha256_file(path)
                if actual != spec["sha256"]:
                    path.unlink(missing_ok=True)
                    raise SystemExit(f"{entry.id}: sha256 {spec['path']} не совпадает — файл удалён")
                verified[spec["path"]] = actual
    receipt = {
        "id": entry.id,
        "repo_id": entry.repo_id,
        "revision": entry.revision,
        "verified_sha256": verified,
        "license": entry.license,
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seconds": round(time.time() - started, 1),
    }
    (target / RECEIPT_NAME).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="models-prepare")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--only", nargs="*", help="ids из model-manifest.json")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.list:
        for entry in manifest.values():
            print(f"{entry.id:45s} role={entry.role:15s} default={entry.prepare_by_default} gated={entry.gated}")
        return 0
    ids = args.only or [e.id for e in manifest.values() if e.prepare_by_default]
    unknown = [i for i in ids if i not in manifest]
    if unknown:
        print(f"Неизвестные модели: {unknown}", file=sys.stderr)
        return 2
    args.models_dir.mkdir(parents=True, exist_ok=True)
    for model_id in ids:
        entry = manifest[model_id]
        print(f"[models-prepare] {entry.id} ({entry.repo_id}) -> {args.models_dir / entry.local_dir}", flush=True)
        receipt = _download_entry(entry, args.models_dir)
        print(f"[models-prepare] OK {entry.id}: verified={list(receipt['verified_sha256'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
