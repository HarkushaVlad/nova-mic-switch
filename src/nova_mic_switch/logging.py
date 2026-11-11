"""Append-only JSON Lines output for raw diagnostics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlWriter:
    def __init__(self, path: Path | None):
        self._stream = path.open("a", encoding="utf-8") if path is not None else None

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=True, sort_keys=True)
        if self._stream is not None:
            self._stream.write(line + "\n")
            self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()


def make_record(mode: str, interface: str, metadata: dict[str, int], label: str,
                elapsed: float, data: bytes | None = None, *, request: bytes | None = None,
                error: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 6),
        "mode": mode,
        "interface": interface,
        "usage_page": metadata["usage_page"],
        "usage": metadata["usage"],
        "interface_number": metadata["interface_number"],
        "label": label,
    }
    if data is not None:
        record["report_length"] = len(data)
        record["data_hex"] = data.hex()
    if request is not None:
        record["request_length"] = len(request)
        record["request_hex"] = request.hex()
    if error is not None:
        record["error"] = error
    return record
