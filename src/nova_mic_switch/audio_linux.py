"""Narrow PulseAudio/PipeWire-Pulse routing through the pactl CLI."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

from .audio_common import AudioError, AudioSource

DISCORD_NAMES = frozenset({
    "discord", "discordcanary", "discordptb", "discord canary", "discord ptb",
})
COMMAND_TIMEOUT_SECONDS = 5
Runner = Callable[..., subprocess.CompletedProcess[str]]


class LinuxAudioRouter:
    def __init__(self, scope: str = "discord", runner: Runner | None = None):
        if scope not in {"discord", "system"}:
            raise AudioError("Linux audio scope must be 'discord' or 'system'")
        self.scope = scope
        self._runner = runner or subprocess.run

    def _run(self, arguments: list[str]) -> str:
        try:
            result = self._runner(
                arguments,
                capture_output=True,
                text=True,
                check=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise AudioError("pactl was not found; install/configure PulseAudio or PipeWire-Pulse") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError(f"pactl command timed out after {COMMAND_TIMEOUT_SECONDS}s: {arguments[1:]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise AudioError(f"pactl command failed ({exc.returncode}){suffix}") from exc
        except OSError as exc:
            raise AudioError(f"could not run pactl: {exc}") from exc
        return result.stdout

    def _get_default(self) -> str:
        source = self._run(["pactl", "get-default-source"]).strip()
        if not source:
            raise AudioError("pactl returned an empty default source")
        return source

    @staticmethod
    def _parse_array(text: str, description: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AudioError(f"pactl returned invalid JSON for {description}") from exc
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise AudioError(f"pactl returned an invalid {description} schema")
        return value

    @staticmethod
    def _properties(item: dict[str, Any], description: str) -> dict[str, Any]:
        properties = item.get("properties", {})
        if properties is None:
            return {}
        if not isinstance(properties, dict):
            raise AudioError(f"pactl returned invalid properties for {description}")
        return properties

    def _source_rows(self) -> list[dict[str, Any]]:
        rows = self._parse_array(
            self._run(["pactl", "--format=json", "list", "sources"]), "sources"
        )
        for source in rows:
            if not isinstance(source.get("name"), str) or not source["name"]:
                raise AudioError("pactl source entry is missing a valid name")
            if not isinstance(source.get("index"), int) or isinstance(source.get("index"), bool):
                raise AudioError(f"pactl source {source['name']!r} is missing a valid index")
            self._properties(source, f"source {source['name']!r}")
        return rows

    def list_sources(self) -> list[AudioSource]:
        rows = self._source_rows()
        default = self._get_default()
        sources = []
        for source in rows:
            properties = self._properties(source, f"source {source['name']!r}")
            name = source["name"]
            device_class = properties.get("device.class")
            if name.endswith(".monitor") or device_class == "monitor":
                continue
            description = properties.get("device.description")
            sources.append(AudioSource(
                id=name,
                name=description if isinstance(description, str) and description else name,
                is_default=(name == default),
            ))
        return sources

    def _require_target(self, source_id: str) -> None:
        if not isinstance(source_id, str) or not source_id:
            raise AudioError("selected audio source ID must be a nonempty string")
        present = {source.id for source in self.list_sources()}
        if source_id not in present:
            raise AudioError(
                f"selected audio source is not currently available as an active capture source: {source_id}"
            )

    def _stream_rows(self) -> list[dict[str, Any]]:
        rows = self._parse_array(
            self._run(["pactl", "--format=json", "list", "source-outputs"]),
            "source-outputs",
        )
        for stream in rows:
            index = stream.get("index")
            source = stream.get("source")
            if (not isinstance(index, int) or isinstance(index, bool)
                    or not isinstance(source, int) or isinstance(source, bool)):
                raise AudioError("pactl source-output entry is missing a valid index or source index")
            self._properties(stream, f"source output {index}")
        return rows

    @staticmethod
    def _is_discord(stream: dict[str, Any]) -> bool:
        properties = LinuxAudioRouter._properties(stream, f"source output {stream.get('index')}")
        names = {"application.name", "application.process.binary"}
        for key in names:
            value = properties.get(key)
            if isinstance(value, str) and value:
                normalized = value.casefold()
                if key == "application.process.binary":
                    normalized = os.path.basename(normalized)
                if normalized in DISCORD_NAMES:
                    return True
        return False

    def _discord_streams(self) -> tuple[list[dict[str, Any]], dict[int, str]]:
        sources = self._source_rows()
        source_names = {source["index"]: source["name"] for source in sources}
        streams = [stream for stream in self._stream_rows() if self._is_discord(stream)]
        if not streams:
            raise AudioError(
                "no Discord recording stream found; start Discord voice capture first "
                "or use --scope system"
            )
        for stream in streams:
            if stream["source"] not in source_names:
                raise AudioError(f"Discord stream {stream['index']} refers to an unknown audio source")
        return streams, source_names

    def current_source(self) -> str:
        if self.scope == "system":
            return self._get_default()
        streams, source_names = self._discord_streams()
        current = {source_names[stream["source"]] for stream in streams}
        if len(current) != 1:
            raise AudioError("Discord has recording streams on mixed sources; refusing to guess")
        return next(iter(current))

    def select_source(self, source_id: str) -> dict[str, Any]:
        self._require_target(source_id)
        if self.scope == "system":
            self._run(["pactl", "set-default-source", source_id])
            actual = self._get_default()
            if actual != source_id:
                raise AudioError(f"system default readback mismatch: requested {source_id!r}, got {actual!r}")
            return {"scope": "system", "source_id": source_id, "verified": True}

        streams, source_names = self._discord_streams()
        if len({source_names[stream["source"]] for stream in streams}) != 1:
            raise AudioError("Discord has recording streams on mixed sources; refusing to route ambiguously")
        original_ids = {stream["index"] for stream in streams}
        moved: list[int] = []
        for stream in streams:
            try:
                self._run(["pactl", "move-source-output", str(stream["index"]), source_id])
            except AudioError as exc:
                raise AudioError(
                    f"Discord stream move failed after {len(moved)}/{len(streams)} successes; "
                    f"partial routing may remain: {exc}"
                ) from exc
            moved.append(stream["index"])

        try:
            verified_streams, source_names = self._discord_streams()
        except AudioError as exc:
            raise AudioError(
                f"Discord streams disappeared or could not be re-read after moving "
                f"{len(moved)}/{len(streams)} streams; partial routing may remain: {exc}"
            ) from exc
        final_ids = {stream["index"] for stream in verified_streams}
        if final_ids != original_ids:
            missing = sorted(original_ids - final_ids)
            added = sorted(final_ids - original_ids)
            raise AudioError(
                "Discord recording streams changed during routing; refusing to assume success "
                f"(disappeared={missing}, appeared={added})"
            )
        wrong = [stream["index"] for stream in verified_streams
                 if source_names[stream["source"]] != source_id]
        if wrong:
            raise AudioError(f"Discord stream readback did not reach {source_id}; streams={wrong}")
        return {
            "scope": "discord",
            "source_id": source_id,
            "moved_streams": sorted(moved),
            "verified": True,
        }
