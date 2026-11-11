"""Pure decoder for the Nova 5P's four-byte media bitmap reports."""

from __future__ import annotations

REPORT_LENGTH = 4
SUPPORTED_MASK = 0x703

_BIT_EVENTS = (
    (0, "volume-up"),
    (1, "volume-down"),
    (8, "previous-track"),
    (9, "play-pause"),
    (10, "next-track"),
)


class MediaReportDecoder:
    """Return supported key-down edges, suppressing repeats until release."""

    def __init__(self) -> None:
        self._previous = 0

    def decode(self, report: bytes) -> tuple[str, ...]:
        if len(report) != REPORT_LENGTH:
            raise ValueError(f"media report must be exactly {REPORT_LENGTH} bytes")
        bitmap = int.from_bytes(report, byteorder="little", signed=False)
        unsupported = bitmap & ~SUPPORTED_MASK
        if unsupported:
            raise ValueError(f"unsupported nonzero media padding bits: 0x{unsupported:08x}")
        rising = bitmap & ~self._previous
        self._previous = bitmap
        return tuple(event for bit, event in _BIT_EVENTS if rising & (1 << bit))
