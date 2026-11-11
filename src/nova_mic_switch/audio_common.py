"""Shared capture-source contract for platform-specific audio adapters."""
from dataclasses import dataclass
from typing import Any, Protocol


class AudioError(RuntimeError):
    """Audio routing failed or could not be safely established."""


@dataclass(frozen=True)
class AudioSource:
    id: str
    name: str
    is_default: bool = False


class AudioRouter(Protocol):
    def list_sources(self) -> list[AudioSource]: ...
    def current_source(self) -> str: ...
    def select_source(self, source_id: str) -> dict[str, Any]: ...
