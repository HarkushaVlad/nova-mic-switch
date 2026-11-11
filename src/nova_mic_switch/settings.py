"""Versioned, side-effect-free application settings persistence."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .audio_factory import default_scope

SETTINGS_VERSION = 1
TRIGGERS = frozenset({"previous-track", "next-track", "play-pause"})


class SettingsError(ValueError):
    """Settings are invalid or cannot be loaded/saved safely."""


@dataclass(frozen=True)
class AppSettings:
    desktop_source: str = ""
    headset_source: str = ""
    trigger: str = "previous-track"
    scope: str = field(default_factory=default_scope)
    feedback_enabled: bool = True
    feedback_output_id: str = ""
    feedback_volume: float = 0.25
    start_at_login: bool = field(default_factory=lambda: sys.platform == "win32")
    start_minimized: bool = True
    auto_start: bool = True


def _valid_scopes() -> frozenset[str]:
    if sys.platform.startswith("linux"):
        return frozenset({"discord", "system"})
    if sys.platform == "win32":
        return frozenset({"system"})
    return frozenset()


def validate_settings(settings: AppSettings, *, require_sources: bool = False) -> None:
    if not isinstance(settings, AppSettings):
        raise SettingsError("settings must be an AppSettings instance")

    for field_name in ("desktop_source", "headset_source", "trigger", "scope", "feedback_output_id"):
        if type(getattr(settings, field_name)) is not str:
            raise SettingsError(f"{field_name} must be a string")
    for field_name in ("feedback_enabled", "start_at_login", "start_minimized", "auto_start"):
        if type(getattr(settings, field_name)) is not bool:
            raise SettingsError(f"{field_name} must be a boolean")

    if settings.trigger not in TRIGGERS:
        raise SettingsError(f"unsupported trigger: {settings.trigger!r}")
    scopes = _valid_scopes()
    if settings.scope not in scopes:
        supported = ", ".join(sorted(scopes)) or "none on this platform"
        raise SettingsError(f"scope {settings.scope!r} is invalid for this platform; supported: {supported}")

    volume = settings.feedback_volume
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise SettingsError("feedback_volume must be a finite number from 0 to 1")
    if not math.isfinite(volume) or not 0 <= volume <= 1:
        raise SettingsError("feedback_volume must be a finite number from 0 to 1")

    if require_sources:
        if not settings.desktop_source or not settings.headset_source:
            raise SettingsError("desktop_source and headset_source must both be selected")
        if settings.desktop_source == settings.headset_source:
            raise SettingsError("desktop_source and headset_source must be distinct")


def load_settings(config_dir: Path) -> AppSettings:
    path = Path(config_dir) / "settings.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AppSettings()
    except (OSError, UnicodeError) as exc:
        raise SettingsError(f"cannot read settings file {path}: {exc}") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"settings file {path} contains invalid JSON; it was not changed") from exc
    if not isinstance(document, dict):
        raise SettingsError(f"settings file {path} must contain a JSON object; it was not changed")
    version = document.get("version")
    if type(version) is not int or version != SETTINGS_VERSION:
        raise SettingsError(
            f"settings file {path} has unsupported version {version!r}; it was not changed"
        )
    values = document.get("settings")
    if not isinstance(values, dict):
        raise SettingsError(f"settings file {path} is missing its settings object; it was not changed")

    known = {field.name for field in fields(AppSettings)}
    try:
        settings = AppSettings(**{key: value for key, value in values.items() if key in known})
        validate_settings(settings)
    except (TypeError, SettingsError) as exc:
        raise SettingsError(f"settings file {path} is invalid; it was not changed: {exc}") from exc
    return settings


def save_settings(config_dir: Path, settings: AppSettings) -> None:
    validate_settings(settings)
    directory = Path(config_dir)
    path = directory / "settings.json"
    temporary_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".settings.", suffix=".tmp", dir=directory
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": SETTINGS_VERSION, "settings": asdict(settings)},
                      stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, TypeError, ValueError) as exc:
        raise SettingsError(f"cannot save settings file {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
