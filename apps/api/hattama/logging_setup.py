"""Structured JSON logs. Meeting content (text, transcripts, prompts) and secrets are never logged:
extra fields with sensitive names are redacted, and code logs only identifiers and metrics."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

SENSITIVE_KEYS = {"text", "transcript", "prompt", "token", "password", "cookie", "authorization", "quote",
                  "content", "messages", "segments", "csrf", "code"}
_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STD or key.startswith("_"):
                continue
            payload[key] = "[redacted]" if key.lower() in SENSITIVE_KEYS else value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_hattama_configured", False):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("HATTAMA_LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "faster_whisper", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._hattama_configured = True  # type: ignore[attr-defined]


def enforce_offline_env() -> None:
    """Runtime never downloads models or sends telemetry (defence in depth; network policy is primary)."""
    for key, value in {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_DATASETS_OFFLINE": "1",
        "DO_NOT_TRACK": "1",
        "PYANNOTE_METRICS_ENABLED": "0",
    }.items():
        os.environ[key] = value
