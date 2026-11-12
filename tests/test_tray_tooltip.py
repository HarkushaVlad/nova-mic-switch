import tempfile
import threading
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from nova_mic_switch.audio_common import AudioSource
from nova_mic_switch.desktop import DesktopWindow


class FakeRouter:
    def __init__(self, current="actual-mic"):
        self.current = current

    def list_sources(self):
        return [AudioSource("actual-mic", "HyperX SoloCast"), AudioSource("other", "Arctis Nova 5")]

    def current_source(self):
        return self.current


class FakeTray:
    def __init__(self):
        self.tooltip = ""

    def setToolTip(self, tooltip):
        self.tooltip = tooltip

    def isVisible(self):
        return False

    def hide(self):
        pass


class CapturingPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def run_next(self):
        task = self.tasks.pop(0)
        thread = threading.Thread(target=task.run)
        thread.start()
        return thread


class CapturingSignal:
    def __init__(self):
        self.args = None
        self.thread_id = None

    def emit(self, *args):
        self.args = args
        self.thread_id = threading.get_ident()


class TrayTooltipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, directory, router_factory=None):
        window = DesktopWindow(Path(directory), safe_mode=True, tray_available=False,
                             router_factory=router_factory or (lambda *_: FakeRouter()))
        window.scope_combo.setCurrentIndex(window.scope_combo.findData("system"))
        window.tray = FakeTray()
        window._tray_refresh_needed = False
        return window

    def finish_task(self, window, thread):
        thread.join(1)
        self.assertFalse(thread.is_alive())
        args = window.signals.tray_source_done.args
        self.assertIsNotNone(args)
        self.assertNotEqual(window.signals.tray_source_done.thread_id, threading.get_ident())
        window._tray_source_loaded(*args)

    def test_tooltip_uses_actual_active_name_not_selected_or_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window.settings = window.settings.__class__(desktop_source="configured", headset_source="other", trigger="next-track")
            window._tray_request_id = 1
            window._tray_query_pending = True
            window._tray_refresh_needed = False
            window._tray_source_loaded(1, (window._tray_generation, ("system", FakeRouter().list_sources(), "actual-mic")), None)
            self.assertEqual(window.tray.tooltip, "Nova Mic Switch · System microphone: HyperX SoloCast")
            window.close()

    def test_external_change_refreshes_and_failure_never_keeps_success(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            sources = FakeRouter().list_sources()
            window._tray_request_id = 1
            window._tray_query_pending = True
            window._tray_refresh_needed = False
            window._tray_source_loaded(1, (window._tray_generation, ("system", sources, "other")), None)
            self.assertIn("System microphone: Arctis Nova 5", window.tray.tooltip)
            window._tray_request_id = 2
            window._tray_query_pending = True
            window._tray_refresh_needed = False
            window._tray_source_loaded(2, (window._tray_generation, None), "query failed")
            self.assertIn("unavailable", window.tray.tooltip)
            window.close()

    def test_dry_run_does_not_claim_a_switch_and_scope_order_is_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window.pool = CapturingPool()
            window.settings = window.settings.__class__(desktop_source="configured", headset_source="other")
            window._service_event({"type": "switch", "applied": False, "dry_run": True, "target_source": "other"})
            self.assertEqual(window.tray.tooltip, "")
            window.close()

    def test_safe_mode_and_shutdown_do_not_query_router(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            window = self.make_window(directory, lambda *_: calls.append("router"))
            window._refresh_tray_source()
            window._finish_close()
            self.assertEqual(calls, [])
            self.assertFalse(window._tray_timer.isActive())

    def test_scope_changes_coalesce_and_old_result_cannot_win(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            pool = CapturingPool()
            window.pool = pool
            window.signals.tray_source_done = CapturingSignal()
            window.router_factory = lambda scope: FakeRouter("other" if scope == "system" else "actual-mic")
            window._refresh_tray_source()
            self.assertEqual(len(pool.tasks), 1)
            window._tray_scope_changed()
            window._tray_scope_changed()
            self.assertEqual(len(pool.tasks), 1)
            self.assertTrue(window._tray_query_pending)
            first_thread = pool.run_next()
            self.finish_task(window, first_thread)
            self.assertIn("unavailable", window.tray.tooltip)
            self.assertEqual(len(pool.tasks), 1)
            second_thread = pool.run_next()
            self.finish_task(window, second_thread)
            self.assertIn("System microphone: Arctis Nova 5", window.tray.tooltip)
            self.assertEqual(len(pool.tasks), 0)
            window.close()

    def test_periodic_tick_during_query_does_not_discard_or_repeat_result(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            pool = CapturingPool()
            window.pool = pool
            window.signals.tray_source_done = CapturingSignal()
            window._refresh_tray_source()
            self.assertEqual(len(pool.tasks), 1)
            window._refresh_tray_source()
            window._refresh_tray_source()
            self.assertFalse(window._tray_refresh_needed)
            self.assertEqual(len(pool.tasks), 1)
            thread = pool.run_next()
            self.finish_task(window, thread)
            self.assertIn("System microphone: HyperX SoloCast", window.tray.tooltip)
            self.assertFalse(window._tray_query_pending)
            self.assertEqual(pool.tasks, [])
            window.close()

    def test_verified_switch_and_route_error_invalidate_inflight_query(self):
        for route_error in (False, True):
            with self.subTest(route_error=route_error), tempfile.TemporaryDirectory() as directory:
                window = self.make_window(directory)
                window.safe_mode = False
                pool = CapturingPool()
                window.pool = pool
                window.signals.tray_source_done = CapturingSignal()
                window.router_factory = lambda *_: FakeRouter("other")
                window._refresh_tray_source()
                old_thread = pool.run_next()
                if route_error:
                    window._service_event({"type": "error", "operation": "route", "message": "failed"})
                else:
                    window._service_event({"type": "switch", "applied": True, "dry_run": False, "target_source": "other"})
                self.assertIn("unavailable", window.tray.tooltip)
                self.assertTrue(window._tray_query_pending)
                self.finish_task(window, old_thread)
                self.assertEqual(len(pool.tasks), 1)
                self.assertIn("unavailable", window.tray.tooltip)
                fresh_thread = pool.run_next()
                self.finish_task(window, fresh_thread)
                self.assertIn("Arctis Nova 5", window.tray.tooltip)
                window.close()

    def test_shutdown_drops_completed_inflight_result_without_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            pool = CapturingPool()
            window.pool = pool
            window.signals.tray_source_done = CapturingSignal()
            window._refresh_tray_source()
            task = pool.tasks.pop(0)
            window._finish_close()
            thread = threading.Thread(target=task.run)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(window.signals.tray_source_done.args)
            self.assertEqual(pool.tasks, [])


if __name__ == "__main__":
    unittest.main()
