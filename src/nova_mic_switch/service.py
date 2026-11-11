"""Background HID trigger service with explicit, pair-bounded audio routing."""

from __future__ import annotations

import importlib
import math
import sys
import threading
from collections.abc import Callable
from typing import Any

from .audio_common import AudioRouter
from .audio_controller import SourceToggleController
from .audio_factory import create_audio_router
from .device import (
    DeviceError,
    HidBackend,
    choose_device,
    enumerate_devices,
    open_interface,
)
from .media import MediaReportDecoder
from .settings import AppSettings, SettingsError, validate_settings


def _load_hid_backend() -> HidBackend:
    if sys.platform.startswith("linux"):
        module_name = "hidraw"
    elif sys.platform == "win32":
        module_name = "hid"
    else:
        raise DeviceError(f"HID backend is unsupported on platform {sys.platform}")
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise DeviceError(f"HID backend {module_name!r} is unavailable") from exc


class SwitchService:
    def __init__(
        self,
        settings: AppSettings,
        on_event: Callable[[dict[str, Any]], None],
        *,
        apply: bool = False,
        backend_factory: Callable[[], HidBackend] | None = None,
        router_factory: Callable[..., AudioRouter] = create_audio_router,
        read_timeout_ms: int = 250,
        reconnect_delay: float = 2.0,
    ):
        if type(apply) is not bool:
            raise ValueError("apply must be a boolean")
        if type(read_timeout_ms) is not int or read_timeout_ms <= 0:
            raise ValueError("read_timeout_ms must be a positive integer")
        if (isinstance(reconnect_delay, bool) or not isinstance(reconnect_delay, (int, float))
                or not math.isfinite(reconnect_delay) or reconnect_delay <= 0):
            raise ValueError("reconnect_delay must be finite and greater than zero")
        self.settings = settings
        self.apply = apply
        self.on_event = on_event
        self.backend_factory = backend_factory or _load_hid_backend
        self.router_factory = router_factory
        self.read_timeout_ms = read_timeout_ms
        self.reconnect_delay = float(reconnect_delay)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = threading.Event()
            thread = threading.Thread(target=self._worker, name="nova-mic-switch", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 3.0) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite nonnegative number")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be a finite nonnegative number")
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return True
            self._stop_event.set()
        if thread is threading.current_thread():
            return False
        thread.join(float(timeout))
        return not thread.is_alive()

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self.on_event(event)
        except BaseException:
            # A dead UI callback must not strand an opened HID handle or recurse.
            pass

    def _worker(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._emit({"type": "state", "state": "connecting"})
                try:
                    self._connect_and_listen()
                except SettingsError as exc:
                    if not self._stop_event.is_set():
                        self._emit({"type": "error", "operation": "configuration",
                                    "message": str(exc), "recoverable": False})
                    break
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._emit({"type": "error", "operation": "device",
                                "message": str(exc), "recoverable": True})
                    self._stop_event.wait(self.reconnect_delay)
        finally:
            self._emit({"type": "state", "state": "stopped"})

    def _connect_and_listen(self) -> None:
        validate_settings(self.settings, require_sources=True)
        try:
            router = self.router_factory(scope=self.settings.scope)
            sources = router.list_sources()
            available = {source.id for source in sources}
        except Exception as exc:
            raise DeviceError(f"could not inspect configured audio sources; waiting to retry: {exc}") from exc
        for source_id in (self.settings.desktop_source, self.settings.headset_source):
            if source_id not in available:
                raise DeviceError(
                    f"configured capture source is currently unavailable: {source_id}; waiting to retry"
                )

        backend = self.backend_factory()
        selected = choose_device(enumerate_devices(backend), "media")
        device = open_interface(backend, selected, "media")
        decoder = MediaReportDecoder()
        controller = SourceToggleController(
            router,
            self.settings.desktop_source,
            self.settings.headset_source,
            apply=self.apply,
        )
        self._emit({"type": "state", "state": "listening"})
        try:
            while not self._stop_event.is_set():
                try:
                    report = device.read(128, self.read_timeout_ms)
                except Exception as exc:
                    raise DeviceError(f"HID media read failed: {exc}") from exc
                if self._stop_event.is_set():
                    break
                if not report:
                    continue
                try:
                    events = decoder.decode(bytes(report))
                except ValueError as exc:
                    self._emit({"type": "error", "operation": "decode",
                                "message": str(exc), "recoverable": True})
                    continue
                if self.settings.trigger not in events:
                    continue
                self._emit({"type": "trigger", "trigger": self.settings.trigger})
                if self._stop_event.is_set():
                    break
                try:
                    result = controller.trigger()
                except Exception as exc:
                    self._emit({"type": "error", "operation": "route",
                                "message": str(exc), "recoverable": True})
                    continue
                self._emit({"type": "switch", **result})
        finally:
            device.close()
