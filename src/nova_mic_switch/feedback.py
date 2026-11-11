"""Quiet, explicit audio cues for confirmed microphone-route changes.

All Qt objects in this module must be created and used on the GUI thread.
WAV generation is deliberately dependency-free and never opens an audio device.
"""

from __future__ import annotations

import base64
import math
import struct
import wave
from pathlib import Path
from typing import Callable

SAMPLE_RATE = 22_050
_PATTERNS = {
    "headset": ((660, 0.075), (880, 0.09)),
    "desktop": ((520, 0.075), (390, 0.09)),
    "error": ((330, 0.15),),
    "test": ((600, 0.08),),
}


def encode_output_id(device_id: bytes) -> str:
    """Return the stable, JSON-safe representation used by settings."""
    return base64.b64encode(bytes(device_id)).decode("ascii")


def decode_output_id(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid saved audio-output ID") from exc


def write_cue(path: Path, cue: str) -> Path:
    """Create a modest-amplitude mono WAV cue at *path*."""
    if cue not in _PATTERNS:
        raise ValueError(f"Unknown cue: {cue}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for frequency, duration in _PATTERNS[cue]:
            count = int(SAMPLE_RATE * duration)
            samples = bytearray()
            for i in range(count):
                # Soft 5 ms ramps avoid clicks; peak is intentionally quiet.
                ramp = min(1.0, i / 110, (count - i) / 110)
                value = int(32767 * 0.16 * max(0.0, ramp) * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE))
                samples.extend(struct.pack("<h", value))
            wav.writeframes(samples)
            if cue in ("headset", "desktop"):
                wav.writeframes(b"\0\0" * int(SAMPLE_RATE * 0.035))
    return path


class AudioFeedback:
    """Qt-thread audio cue player pinned to an explicitly selected output.

    The player reports readiness, not whether sound was physically audible.
    A missing selected endpoint is never replaced with the system default.
    """

    def __init__(self, config_dir: Path, *, output_id: str = "", enabled: bool = True,
                 volume: float = 0.25, warning: Callable[[str], None] | None = None,
                 devices=None, sound_effect_factory=None):
        from PySide6.QtCore import QObject
        from PySide6.QtMultimedia import QMediaDevices

        self.config_dir = Path(config_dir)
        self.enabled = enabled
        self.volume = max(0.0, min(1.0, float(volume)))
        self.warning = warning or (lambda _message: None)
        self.devices = devices if devices is not None else QMediaDevices()
        if sound_effect_factory is None:
            from PySide6.QtMultimedia import QSoundEffect
            sound_effect_factory = QSoundEffect
        self._sound_effect_factory = sound_effect_factory
        self._owner = QObject()
        self.output_id = output_id
        self._effects: dict[str, object] = {}
        self._paths = {key: write_cue(self.config_dir / "cues" / f"{key}.wav", key) for key in _PATTERNS}
        self._device: QAudioDevice | None = None
        self._refresh_output()
        self.devices.audioOutputsChanged.connect(self._refresh_output)

    @staticmethod
    def outputs() -> list:
        from PySide6.QtMultimedia import QMediaDevices
        return list(QMediaDevices.audioOutputs())

    def set_output(self, output_id: str) -> None:
        self.output_id = output_id
        self._refresh_output()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if self.enabled:
            self._refresh_output()
        else:
            self._dispose_effects()

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, float(volume)))
        for effect in self._effects.values():
            effect.setVolume(self.volume)

    def _refresh_output(self) -> None:
        self._dispose_effects()
        self._device = None
        if self.output_id:
            try:
                wanted = decode_output_id(self.output_id)
                self._device = next(
                    (d for d in self.devices.audioOutputs() if bytes(d.id()) == wanted), None
                )
            except ValueError:
                pass
        if self.output_id and self._device is None:
            self.warning("The selected sound output is unavailable. Choose another output; cues will not use your default speakers.")
            return
        if self._device is not None and self.enabled:
            self._preload_effects()

    def _dispose_effects(self) -> None:
        for effect in self._effects.values():
            effect.stop()
            effect.deleteLater()
        self._effects.clear()

    def _preload_effects(self) -> None:
        from PySide6.QtCore import QUrl

        for cue in _PATTERNS:
            effect = self._sound_effect_factory(self._owner)
            effect.setAudioDevice(self._device)
            effect.setVolume(self.volume)
            effect.setSource(QUrl.fromLocalFile(str(self._paths[cue])))
            effect.statusChanged.connect(
                lambda cue=cue, effect=effect: self._status_changed(cue, effect)
            )
            self._effects[cue] = effect

    def _status_changed(self, cue: str, effect) -> None:
        if effect.status() == effect.Status.Error and self._effects.get(cue) is effect:
            self.warning(f"The {cue} sound cue could not be loaded for the selected output.")

    def play(self, cue: str) -> bool:
        if cue not in _PATTERNS:
            raise ValueError(f"Unknown cue: {cue}")
        if not self.enabled:
            return False
        if self._device is None:
            if not self.output_id:
                self.warning("Choose a sound output in advanced settings to enable cues.")
            return False
        effect = self._effects.get(cue)
        if effect is None:
            return False
        if effect.status() == effect.Status.Error:
            self.warning(f"The {cue} sound cue could not be loaded for the selected output.")
            return False
        if effect.status() != effect.Status.Ready:
            self.warning(f"The {cue} sound cue is still loading for the selected output.")
            return False
        effect.play()
        return True

    def route_succeeded(self, event: dict, *, desktop_source: str, headset_source: str) -> bool:
        """Play only for backend-verified, actually-applied route events."""
        if event.get("type") != "switch" or event.get("applied") is not True:
            return False
        if event.get("dry_run") is True:
            return False
        target = event.get("target_source")
        cue = "headset" if target == headset_source else "desktop" if target == desktop_source else None
        return self.play(cue) if cue else False

    def close(self) -> None:
        try:
            self.devices.audioOutputsChanged.disconnect(self._refresh_output)
        except (RuntimeError, TypeError, ValueError):
            pass
        self._dispose_effects()
