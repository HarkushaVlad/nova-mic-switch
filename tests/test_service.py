import threading
import time
import unittest

from nova_mic_switch.audio_common import AudioError, AudioSource
from nova_mic_switch.device import EXPECTED_INTERFACES, PRODUCT_ID, VENDOR_ID
from nova_mic_switch.service import SwitchService
from nova_mic_switch.settings import AppSettings


DESKTOP = "desktop-source"
HEADSET = "headset-source"


def media_row():
    interface = EXPECTED_INTERFACES["media"]
    return {"vendor_id": VENDOR_ID, "product_id": PRODUCT_ID,
            "usage_page": interface["usage_page"], "usage": interface["usage"],
            "interface_number": interface["interface_number"], "path": b"media-path"}


class FakeHandle:
    def __init__(self, reports=(), *, fail_after_reports=False):
        self.reports = list(reports)
        self.fail_after_reports = fail_after_reports
        self.opened = []
        self.closed = False
        self.read_calls = []
        self.write_calls = []
        self.open_event = threading.Event()

    def open_path(self, path):
        self.opened.append(path)
        self.open_event.set()

    def read(self, length, timeout):
        self.read_calls.append((length, timeout))
        if self.reports:
            value = self.reports.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        if self.fail_after_reports:
            self.fail_after_reports = False
            raise OSError("simulated HID disconnect")
        time.sleep(timeout / 1000)
        return []

    def write(self, data):
        self.write_calls.append(data)
        return len(data)

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, handle, rows=None):
        self.handle = handle
        self.rows = [media_row()] if rows is None else rows
        self.device_calls = 0
        self.enumerations = []

    def enumerate(self, vid, pid):
        self.enumerations.append((vid, pid))
        return self.rows

    def device(self):
        self.device_calls += 1
        return self.handle


class FakeRouter:
    def __init__(self, sources=(DESKTOP, HEADSET), current=DESKTOP):
        self.sources = [AudioSource(source, source) for source in sources]
        self.current = current
        self.selected = []
        self.current_calls = 0
        self.current_errors = 0
        self.list_calls = 0

    def list_sources(self):
        self.list_calls += 1
        return self.sources

    def current_source(self):
        self.current_calls += 1
        if self.current_errors:
            self.current_errors -= 1
            raise AudioError("Discord capture is not active yet")
        return self.current

    def select_source(self, source_id):
        self.selected.append(source_id)
        self.current = source_id
        return {"source_id": source_id, "verified": True}


class Events:
    def __init__(self):
        self.items = []
        self.condition = threading.Condition()

    def __call__(self, event):
        with self.condition:
            self.items.append(event)
            self.condition.notify_all()

    def wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not predicate(self.items):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for event; got {self.items!r}")
                self.condition.wait(remaining)
            return list(self.items)


def settings():
    return AppSettings(desktop_source=DESKTOP, headset_source=HEADSET)


class SwitchServiceTests(unittest.TestCase):
    def test_constructor_is_lazy_and_start_does_not_duplicate_worker(self):
        handle = FakeHandle()
        backend = FakeBackend(handle)
        router = FakeRouter()
        calls = {"backend": 0, "router": 0}

        def backend_factory():
            calls["backend"] += 1
            return backend

        def router_factory(*, scope):
            calls["router"] += 1
            self.assertEqual(scope, settings().scope)
            return router

        events = Events()
        service = SwitchService(settings(), events, backend_factory=backend_factory,
                                router_factory=router_factory, read_timeout_ms=10)
        self.assertEqual(calls, {"backend": 0, "router": 0})
        self.assertFalse(service.running)
        service.start()
        events.wait_for(lambda values: any(e.get("state") == "listening" for e in values))
        service.start()
        time.sleep(0.02)
        self.assertEqual(calls["backend"], 1)
        self.assertTrue(service.running)
        self.assertTrue(service.stop())
        self.assertTrue(handle.closed)
        self.assertFalse(service.running)
        self.assertEqual(events.items[-1], {"type": "state", "state": "stopped"})

    def test_apply_false_emits_trigger_and_dry_run_switch_without_routing(self):
        handle = FakeHandle([list(bytes.fromhex("00010000"))])
        backend = FakeBackend(handle)
        router = FakeRouter()
        events = Events()
        service = SwitchService(settings(), events, backend_factory=lambda: backend,
                                router_factory=lambda **_: router, read_timeout_ms=10)
        service.start()
        events.wait_for(lambda values: any(e.get("type") == "switch" for e in values))
        self.assertTrue(service.stop())
        trigger = next(event for event in events.items if event["type"] == "trigger")
        switch = next(event for event in events.items if event["type"] == "switch")
        self.assertEqual(trigger, {"type": "trigger", "trigger": "previous-track"})
        self.assertFalse(switch["applied"])
        self.assertTrue(switch["dry_run"])
        self.assertEqual(switch["target_source"], HEADSET)
        self.assertEqual(router.selected, [])
        self.assertEqual(handle.write_calls, [])

    def test_apply_true_routes_only_selected_rising_edge(self):
        handle = FakeHandle([
            list(bytes.fromhex("00020000")),
            list(bytes.fromhex("00010000")),
            list(bytes.fromhex("00010000")),
            list(bytes.fromhex("00000000")),
        ])
        backend = FakeBackend(handle)
        router = FakeRouter(current=HEADSET)
        events = Events()
        service = SwitchService(settings(), events, apply=True, backend_factory=lambda: backend,
                                router_factory=lambda **_: router, read_timeout_ms=10)
        service.start()
        events.wait_for(lambda values: any(e.get("type") == "switch" for e in values))
        self.assertTrue(service.stop())
        switch = next(event for event in events.items if event["type"] == "switch")
        self.assertTrue(switch["applied"])
        self.assertFalse(switch["dry_run"])
        self.assertEqual(router.selected, [DESKTOP])
        self.assertEqual(len([e for e in events.items if e["type"] == "trigger"]), 1)
        self.assertEqual(handle.write_calls, [])

    def test_unavailable_pair_fails_before_backend_or_hid_open(self):
        handle = FakeHandle()
        backend_calls = []
        events = Events()
        service_ref = {}

        def on_event(event):
            events(event)
            if event.get("type") == "error":
                service_ref["service"].stop(timeout=0.01)

        service = SwitchService(settings(), on_event,
                                backend_factory=lambda: backend_calls.append(True),
                                router_factory=lambda **_: FakeRouter(sources=(DESKTOP,)),
                                read_timeout_ms=10)
        service_ref["service"] = service
        service.start()
        events.wait_for(lambda values: any(e.get("type") == "error" for e in values))
        self.assertTrue(service.stop())
        error = next(event for event in events.items if event["type"] == "error")
        self.assertEqual(error["operation"], "device")
        self.assertTrue(error["recoverable"])
        self.assertIn(HEADSET, error["message"])
        self.assertEqual(backend_calls, [])
        self.assertEqual(handle.opened, [])

    def test_unavailable_source_recovery_retries_without_restarting_service(self):
        handle = FakeHandle()
        backend = FakeBackend(handle)
        router = FakeRouter(sources=(DESKTOP,))
        backend_calls = []
        events = Events()
        service_ref = {}

        def on_event(event):
            events(event)
            if event.get("operation") == "device":
                router.sources = [AudioSource(DESKTOP, DESKTOP), AudioSource(HEADSET, HEADSET)]

        service = SwitchService(settings(), on_event,
                                backend_factory=lambda: backend_calls.append(True) or backend,
                                router_factory=lambda **_: router, read_timeout_ms=10,
                                reconnect_delay=0.01)
        service_ref["service"] = service
        service.start()
        observed = events.wait_for(lambda values: any(e.get("state") == "listening" for e in values))
        self.assertTrue(service.stop())
        error = next(e for e in observed if e.get("type") == "error")
        self.assertEqual(error["operation"], "device")
        self.assertTrue(error["recoverable"])
        self.assertEqual(backend_calls, [True])
        self.assertGreaterEqual(router.list_calls, 2)
        self.assertEqual(handle.opened, [b"media-path"])
        self.assertTrue(handle.closed)

    def test_source_enumeration_error_retries_until_sources_are_available(self):
        handle = FakeHandle()
        backend = FakeBackend(handle)
        events = Events()
        enumeration_calls = []

        class TransientRouter(FakeRouter):
            def list_sources(self):
                enumeration_calls.append(True)
                if len(enumeration_calls) == 1:
                    raise AudioError("temporary audio enumeration failure")
                return super().list_sources()

        router = TransientRouter()
        service = SwitchService(settings(), events, backend_factory=lambda: backend,
                                router_factory=lambda **_: router, read_timeout_ms=10,
                                reconnect_delay=0.01)
        service.start()
        observed = events.wait_for(lambda values: any(e.get("state") == "listening" for e in values))
        self.assertTrue(service.stop())
        error = next(e for e in observed if e.get("type") == "error")
        self.assertEqual(error["operation"], "device")
        self.assertTrue(error["recoverable"])
        self.assertIn("temporary audio enumeration failure", error["message"])
        self.assertGreaterEqual(len(enumeration_calls), 2)
        self.assertEqual(backend.device_calls, 1)
        self.assertTrue(handle.closed)

    def test_wrong_usage_is_rejected_by_guard_and_callback_can_stop_worker(self):
        wrong = media_row()
        wrong["usage_page"] = 0xFFC0
        handle = FakeHandle()
        backend = FakeBackend(handle, [wrong])
        events = []
        service_ref = {}
        stop_results = []

        def on_event(event):
            events.append(event)
            if event.get("type") == "error":
                stop_results.append(service_ref["service"].stop(timeout=0.01))

        service = SwitchService(settings(), on_event, backend_factory=lambda: backend,
                                router_factory=lambda **_: FakeRouter(), read_timeout_ms=10)
        service_ref["service"] = service
        service.start()
        deadline = time.monotonic() + 1
        while service.running and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(service.running)
        self.assertTrue(any(e.get("operation") == "device" for e in events))
        self.assertEqual(stop_results, [False])
        self.assertEqual(backend.device_calls, 0)
        self.assertEqual(handle.opened, [])

    def test_route_error_is_recoverable_and_service_keeps_listening(self):
        handle = FakeHandle([
            list(bytes.fromhex("00010000")),
            list(bytes.fromhex("00000000")),
            list(bytes.fromhex("00010000")),
        ])
        backend = FakeBackend(handle)
        router = FakeRouter()
        router.current_errors = 1
        events = Events()
        service = SwitchService(settings(), events, backend_factory=lambda: backend,
                                router_factory=lambda **_: router, read_timeout_ms=10)
        service.start()
        events.wait_for(lambda values: any(e.get("type") == "switch" for e in values))
        self.assertTrue(service.stop())
        route_error = next(e for e in events.items if e.get("operation") == "route")
        self.assertTrue(route_error["recoverable"])
        self.assertTrue(any(e.get("type") == "switch" for e in events.items))
        self.assertEqual(router.selected, [])

    def test_disconnect_closes_reconnects_and_resets_decoder(self):
        first = FakeHandle([list(bytes.fromhex("00010000"))], fail_after_reports=True)
        second = FakeHandle([list(bytes.fromhex("00010000"))])
        backends = [FakeBackend(first), FakeBackend(second)]
        backend_calls = []

        def backend_factory():
            backend = backends[min(len(backend_calls), len(backends) - 1)]
            backend_calls.append(backend)
            return backend

        routers = []

        def router_factory(**_):
            router = FakeRouter()
            routers.append(router)
            return router

        events = Events()
        service = SwitchService(settings(), events, backend_factory=backend_factory,
                                router_factory=router_factory, read_timeout_ms=10,
                                reconnect_delay=0.01)
        service.start()
        events.wait_for(lambda values: len([e for e in values if e.get("type") == "trigger"]) >= 2)
        self.assertTrue(service.stop())
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertGreaterEqual(len(backend_calls), 2)
        self.assertGreaterEqual(len(routers), 2)
        self.assertEqual(len([e for e in events.items if e.get("type") == "trigger"]), 2)
        self.assertTrue(any(e.get("operation") == "device" for e in events.items))

    def test_callback_exception_does_not_prevent_close(self):
        handle = FakeHandle()
        backend = FakeBackend(handle)

        def broken_callback(_event):
            raise RuntimeError("consumer callback failed")

        service = SwitchService(settings(), broken_callback, backend_factory=lambda: backend,
                                router_factory=lambda **_: FakeRouter(), read_timeout_ms=10)
        service.start()
        self.assertTrue(handle.open_event.wait(1))
        self.assertTrue(service.stop())
        self.assertTrue(handle.closed)

    def test_stop_during_blocked_read_discards_returned_trigger_and_closes(self):
        class ControlledHandle(FakeHandle):
            def __init__(self):
                super().__init__()
                self.read_entered = threading.Event()
                self.release_read = threading.Event()

            def read(self, length, timeout):
                self.read_calls.append((length, timeout))
                self.read_entered.set()
                self.release_read.wait(1)
                return list(bytes.fromhex("00010000"))

        handle = ControlledHandle()
        router = FakeRouter()
        events = Events()
        service = SwitchService(settings(), events, apply=True,
                                backend_factory=lambda: FakeBackend(handle),
                                router_factory=lambda **_: router, read_timeout_ms=10)
        service.start()
        self.assertTrue(handle.read_entered.wait(1))
        self.assertFalse(service.stop(timeout=0))
        handle.release_read.set()
        events.wait_for(lambda values: values and values[-1] == {"type": "state", "state": "stopped"})
        self.assertTrue(service.stop())
        self.assertTrue(handle.closed)
        self.assertEqual(router.current_calls, 0)
        self.assertEqual(router.selected, [])
        self.assertFalse(any(e.get("type") in {"trigger", "switch"} for e in events.items))

    def test_stop_from_trigger_callback_skips_route_and_closes(self):
        handle = FakeHandle([list(bytes.fromhex("00010000"))])
        router = FakeRouter()
        events = []
        service_ref = {}
        stop_results = []

        def on_event(event):
            events.append(event)
            if event.get("type") == "trigger":
                stop_results.append(service_ref["service"].stop(timeout=0.01))

        service = SwitchService(settings(), on_event, apply=True,
                                backend_factory=lambda: FakeBackend(handle),
                                router_factory=lambda **_: router, read_timeout_ms=10)
        service_ref["service"] = service
        service.start()
        deadline = time.monotonic() + 1
        while service.running and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(service.running)
        self.assertEqual(stop_results, [False])
        self.assertTrue(handle.closed)
        self.assertEqual(router.current_calls, 0)
        self.assertEqual(router.selected, [])
        self.assertFalse(any(e.get("type") == "switch" for e in events))

    def test_invalid_configuration_emits_configuration_error_without_factories(self):
        events = Events()
        calls = []
        service = SwitchService(AppSettings(), events,
                                backend_factory=lambda: calls.append("backend"),
                                router_factory=lambda **_: calls.append("router"),
                                read_timeout_ms=10)
        service.start()
        events.wait_for(lambda values: values and values[-1] == {"type": "state", "state": "stopped"})
        error = next(e for e in events.items if e.get("type") == "error")
        self.assertEqual(error["operation"], "configuration")
        self.assertFalse(error["recoverable"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
