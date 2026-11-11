"""Lazy construction of the platform audio backend."""

from __future__ import annotations

import importlib
import sys

from .audio_common import AudioError, AudioRouter


def default_scope() -> str:
    if sys.platform.startswith("linux"):
        return "discord"
    if sys.platform == "win32":
        return "system"
    raise AudioError(f"audio routing is unsupported on platform {sys.platform}")


def create_audio_router(scope: str | None = None) -> AudioRouter:
    actual_scope = scope or default_scope()
    if sys.platform.startswith("linux"):
        from .audio_linux import LinuxAudioRouter

        return LinuxAudioRouter(scope=actual_scope)
    if sys.platform == "win32":
        if actual_scope != "system":
            raise AudioError("Windows supports only --scope system")
        try:
            module = importlib.import_module(".audio_windows", package=__package__)
        except ImportError as exc:
            raise AudioError("Windows audio backend is unavailable") from exc
        return module.WindowsAudioRouter()
    raise AudioError(f"audio routing is unsupported on platform {sys.platform}")
