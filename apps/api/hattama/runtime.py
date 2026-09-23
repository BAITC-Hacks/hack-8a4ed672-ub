"""Construction of local model providers from the active resource profile. Never downloads anything."""

from __future__ import annotations

from typing import Any, Literal

from hattama.asr.base import ASRProvider, AsrUnavailableError
from hattama.config import Settings
from hattama.diagnostics.profiles import load_profiles
from hattama.modelstore.manifest import ModelNotPreparedError, load_manifest, resolve_local_model


def active_profile(settings: Settings) -> dict[str, Any]:
    profiles = load_profiles(settings.profiles_file)
    if settings.profile not in profiles:
        raise ValueError(f"Неизвестный профиль {settings.profile}; доступны: {', '.join(profiles)}")
    return profiles[settings.profile]


def make_asr_provider(settings: Settings, stage: Literal["live", "final"]) -> ASRProvider:
    cfg = active_profile(settings)[f"{stage}_asr"]
    if cfg["model"] == "none":
        raise AsrUnavailableError(f"Профиль {settings.profile} не содержит ASR-модели (тестовый профиль)")
    manifest = load_manifest(settings.model_manifest)
    entry = manifest[cfg["model"]]
    try:
        path = resolve_local_model(entry, settings.models_dir)
    except ModelNotPreparedError as exc:
        raise AsrUnavailableError(str(exc)) from exc
    from hattama.asr.faster_whisper_provider import FasterWhisperProvider

    return FasterWhisperProvider(path, model_id=entry.id, device=cfg["device"], compute_type=cfg["compute_type"])


def participant_glossary(names: list[str]) -> str | None:
    """Names only (no Russian/Kazakh filler words) so the prompt does not bias language detection."""
    cleaned = []
    for name in names:
        name = name.strip()
        if name and name not in cleaned:
            cleaned.append(name)
    return (", ".join(cleaned[:30]) + ".") if cleaned else None
