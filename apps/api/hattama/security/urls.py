"""URL policies: meeting links (SSRF-safe) and internal-only LLM endpoints."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

# Hosts a meeting link may point to (exact host or subdomain). Redirect targets are re-checked by the
# meeting-agent's navigation guard with the same function plus platform auth hosts.
MEETING_HOSTS: dict[str, tuple[str, ...]] = {
    "google_meet": ("meet.google.com",),
    "teams_web": ("teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"),
    "zoom_web": ("zoom.us", "zoom.com", "app.zoom.us"),
}
AUTH_HOSTS: dict[str, tuple[str, ...]] = {
    "google_meet": ("accounts.google.com",),
    "teams_web": ("login.microsoftonline.com", "login.live.com", "login.microsoft.com"),
    "zoom_web": ("zoom.us", "zoom.com"),
}


class MeetingUrlError(ValueError):
    pass


def _host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == a or host.endswith("." + a) for a in allowed)


def _reject_ip_and_local(host: str) -> None:
    if not host or host in {"localhost"} or host.endswith(".localhost") or host.endswith(".local") \
            or host.endswith(".internal"):
        raise MeetingUrlError("Локальные адреса запрещены")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    raise MeetingUrlError("IP-адреса в ссылке встречи запрещены")


def validate_meeting_url(url: str, platform: str, *, allow_auth_hosts: bool = False) -> str:
    url = url.strip()
    if len(url) > 2000:
        raise MeetingUrlError("Слишком длинная ссылка")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise MeetingUrlError("Некорректная ссылка встречи") from exc
    if parts.scheme != "https":
        raise MeetingUrlError("Разрешены только ссылки https://")
    if parts.username or parts.password:
        raise MeetingUrlError("Ссылка не должна содержать учётные данные")
    if port not in (None, 443):
        raise MeetingUrlError("Нестандартный порт запрещён")
    host = (parts.hostname or "").lower().rstrip(".")
    _reject_ip_and_local(host)
    allowed = MEETING_HOSTS.get(platform)
    if allowed is None:
        raise MeetingUrlError(f"Для платформы {platform} ссылка не поддерживается")
    if allow_auth_hosts:
        allowed = allowed + AUTH_HOSTS.get(platform, ())
    if not _host_matches(host, allowed):
        raise MeetingUrlError(f"Хост {host} не относится к платформе {platform}")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def validate_llm_base_url(url: str, allowed_hosts: list[str]) -> str:
    """LLM endpoints are administrator-configured INTERNAL addresses only; never a cloud API."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("LLM endpoint: только http(s)")
    host = (parts.hostname or "").lower()
    if host not in [h.lower() for h in allowed_hosts]:
        raise ValueError(f"LLM endpoint {host} не входит в список разрешённых внутренних адресов")
    try:
        ip = ipaddress.ip_address(host)
        if not (ip.is_loopback or ip.is_private):
            raise ValueError("LLM endpoint должен быть внутренним адресом")
    except ValueError as exc:
        if "LLM endpoint" in str(exc):
            raise
    return url.rstrip("/")
