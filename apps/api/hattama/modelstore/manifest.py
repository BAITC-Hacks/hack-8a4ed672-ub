"""Model manifest: pinned sources, local paths and preparation receipts.

Runtime code resolves models ONLY through `resolve_local_model`, which never downloads.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECEIPT_NAME = ".hattama-model.json"


class ModelNotPreparedError(RuntimeError):
    """Raised when a model is requested at runtime but was not prepared locally."""


@dataclass(frozen=True)
class ModelEntry:
    id: str
    role: str
    repo_id: str
    revision: str | None
    files: list[dict[str, Any]] | str
    local_dir: str
    license: str
    runtime: str
    prepare_by_default: bool
    gated: bool
    raw: dict[str, Any]


def load_manifest(path: Path) -> dict[str, ModelEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, ModelEntry] = {}
    for item in data["models"]:
        entries[item["id"]] = ModelEntry(
            id=item["id"],
            role=item["role"],
            repo_id=item["source"]["repo_id"],
            revision=item["source"].get("revision"),
            files=item.get("files", []),
            local_dir=item["local_dir"],
            license=item.get("license", ""),
            runtime=item.get("runtime", ""),
            prepare_by_default=bool(item.get("prepare", False)),
            gated=bool(item.get("gated", False)),
            raw=item,
        )
    return entries


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def read_receipt(model_dir: Path) -> dict[str, Any] | None:
    receipt = model_dir / RECEIPT_NAME
    if not receipt.is_file():
        return None
    try:
        return json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def model_status(entry: ModelEntry, models_dir: Path) -> dict[str, Any]:
    model_dir = models_dir / entry.local_dir
    receipt = read_receipt(model_dir)
    missing: list[str] = []
    if isinstance(entry.files, list):
        for f in entry.files:
            if not (model_dir / f["path"]).is_file():
                missing.append(f["path"])
    prepared = receipt is not None and not missing
    return {
        "id": entry.id,
        "role": entry.role,
        "path": str(model_dir),
        "prepared": prepared,
        "missing_files": missing,
        "revision": (receipt or {}).get("revision"),
        "verified_sha256": (receipt or {}).get("verified_sha256", {}),
        "license": entry.license,
    }


def resolve_local_model(entry: ModelEntry, models_dir: Path) -> Path:
    status = model_status(entry, models_dir)
    if not status["prepared"]:
        raise ModelNotPreparedError(
            f"Модель {entry.id} не подготовлена в {status['path']}. "
            f"Выполните: python tasks.py models-prepare --only {entry.id}. "
            f"Отсутствуют: {', '.join(status['missing_files']) or 'квитанция подготовки'}"
        )
    return Path(status["path"])
