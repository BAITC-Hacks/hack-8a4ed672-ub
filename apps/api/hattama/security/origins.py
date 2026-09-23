"""Origin checks for WebSockets and unsafe HTTP methods. CORS is NOT an authorization mechanism."""

from __future__ import annotations

import re

from hattama.config import Settings

_EXT_ID = re.compile(r"^[a-p]{32}$")


def allowed_web_origin(origin: str | None, settings: Settings) -> bool:
    return bool(origin) and origin in settings.allowed_origins


def allowed_extension_origin(origin: str | None, settings: Settings) -> bool:
    if not origin or not origin.startswith("chrome-extension://"):
        return False
    ext_id = origin.removeprefix("chrome-extension://").rstrip("/")
    return bool(_EXT_ID.match(ext_id)) and ext_id in settings.allowed_extension_ids


def ingest_origin_kind(origin: str | None, settings: Settings) -> str | None:
    """Returns 'web' | 'extension' | 'agent' | None (reject).

    The meeting-agent is a server-side client without a browser Origin; it must authenticate with an
    ingest token, so a missing Origin is accepted only as 'agent' (token required later).
    """
    if origin is None or origin == "":
        return "agent"
    if allowed_web_origin(origin, settings):
        return "web"
    if allowed_extension_origin(origin, settings):
        return "extension"
    return None
