"""Pure routing decisions for explicit source-pair toggling."""

from __future__ import annotations

from typing import Any

from .audio_common import AudioError, AudioRouter


def opposite_source(current: str, desktop_source: str, headset_source: str) -> str:
    if not desktop_source or not headset_source or desktop_source == headset_source:
        raise AudioError("desktop and headset source IDs must be distinct and nonempty")
    if current == desktop_source:
        return headset_source
    if current == headset_source:
        return desktop_source
    raise AudioError(
        f"current source {current!r} is outside the configured pair; refusing to hijack another source"
    )


class SourceToggleController:
    def __init__(self, router: AudioRouter, desktop_source: str, headset_source: str,
                 *, apply: bool = False):
        if not desktop_source or not headset_source or desktop_source == headset_source:
            raise AudioError("desktop and headset source IDs must be distinct and nonempty")
        self.router = router
        self.desktop_source = desktop_source
        self.headset_source = headset_source
        self.apply = apply

    def trigger(self) -> dict[str, Any]:
        current = self.router.current_source()
        target = opposite_source(current, self.desktop_source, self.headset_source)
        if not self.apply:
            return {
                "current_source": current,
                "target_source": target,
                "applied": False,
                "dry_run": True,
            }
        result = self.router.select_source(target)
        return {
            "current_source": current,
            "target_source": target,
            "applied": True,
            "dry_run": False,
            "routing_result": result,
        }
