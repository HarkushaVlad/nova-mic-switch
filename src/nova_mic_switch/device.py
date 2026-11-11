"""Exact VID/PID and usage-gated HID access; no automatic interface fallback."""

from __future__ import annotations

from typing import Any, Protocol

VENDOR_ID = 0x1038
PRODUCT_ID = 0x2232
STATUS_REQUEST = b"\x00\xb0"

EXPECTED_INTERFACES: dict[str, dict[str, int]] = {
    "status": {"usage_page": 0xFFC0, "usage": 0x01, "interface_number": 3},
    "media": {"usage_page": 0x000C, "usage": 0x01, "interface_number": 4},
    "vendor": {"usage_page": 0xFF00, "usage": 0x01, "interface_number": 5},
}


class DeviceError(Exception):
    """An expected device selection or HID operation failure."""


class DeviceHandle(Protocol):
    def open_path(self, path: Any) -> None: ...
    def read(self, max_length: int, timeout_ms: int) -> list[int]: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class HidBackend(Protocol):
    def enumerate(self, vendor_id: int, product_id: int) -> list[dict[str, Any]]: ...
    def device(self) -> DeviceHandle: ...


def enumerate_devices(backend: HidBackend) -> list[dict[str, Any]]:
    try:
        # Exact VID/PID arguments plus a defensive post-filter.
        return [item for item in backend.enumerate(VENDOR_ID, PRODUCT_ID)
                if item.get("vendor_id") == VENDOR_ID and item.get("product_id") == PRODUCT_ID]
    except Exception as exc:
        raise DeviceError(f"HID enumeration failed: {exc}") from exc


def choose_device(devices: list[dict[str, Any]], interface: str) -> dict[str, Any]:
    expected = EXPECTED_INTERFACES[interface]
    matches = [item for item in devices
               if item.get("usage_page") == expected["usage_page"]
               and item.get("usage") == expected["usage"]]
    for item in matches:
        number = item.get("interface_number")
        if number is not None and number != expected["interface_number"]:
            raise DeviceError(
                f"{interface} usage matched unexpected interface number {number}; refusing to open"
            )
    if not matches:
        raise DeviceError(f"no matching {interface} interface found for 1038:2232")
    if len(matches) != 1:
        raise DeviceError(f"ambiguous {interface} interface: found {len(matches)} usage matches; refusing")
    if matches[0].get("path") is None:
        raise DeviceError(f"matching {interface} interface has no HID path")
    return matches[0]


def open_interface(backend: HidBackend, selected: dict[str, Any], interface: str) -> DeviceHandle:
    handle = backend.device()
    try:
        handle.open_path(selected["path"])
    except Exception as exc:
        try:
            handle.close()
        except Exception:
            pass
        text = str(exc)
        if "permission" in text.lower() or "access denied" in text.lower():
            raise DeviceError(
                f"cannot open {interface} HID interface: permission denied. No privileged retry was attempted."
            ) from exc
        raise DeviceError(f"cannot open {interface} HID interface: {exc}") from exc
    return handle
