import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from nova_mic_switch.audio_common import AudioSource
from nova_mic_switch.desktop import DesktopWindow
from nova_mic_switch.settings import AppSettings, save_settings


class FakeRouter:
    def list_sources(self):
        return [AudioSource("system-mic", "Built-in microphone", True), AudioSource("usb-mic", "USB headset microphone"), AudioSource("nova-mic", "Nova 5P microphone")]

    def current_source(self):
        return "system-mic"


class FakeFeedback:
    all_outputs = []
    instances = []
    output_calls = 0

    @classmethod
    def outputs(cls):
        cls.output_calls += 1
        return cls.all_outputs

    def __init__(self, *_args, **_kwargs):
        self.played = []
        self.output = _kwargs.get("output_id", "")
        self.__class__.instances.append(self)

    def play(self, cue):
        self.played.append(cue)
        return True

    def set_output(self, _output): self.output = _output
    def set_enabled(self, _enabled): pass
    def set_volume(self, _volume): pass
    def close(self): pass
    def route_succeeded(self, event, **kwargs):
        self.played.append(("route", event, kwargs))
        return True


class FakeOutput:
    def __init__(self, name, identifier): self.name, self.identifier = name, identifier
    def description(self): return self.name
    def id(self): return self.identifier


class FakeService:
    instances = []
    def __init__(self, settings, callback, **_kwargs):
        self.settings, self.callback = settings, callback
        self.running = False
        self.stop_result = False
        self.stop_calls = 0
        self.__class__.instances.append(self)
    def start(self): self.running = True
    def stop(self, timeout=3):
        self.stop_calls += 1
        if self.stop_result:
            self.running = False
        return self.stop_result


class FakeStartup:
    available = True
    def __init__(self, enabled=False, *, fail_write=False, fail_read_after_write=False):
        self.enabled, self.calls = enabled, []
        self.minimized = True
        self.fail_write = fail_write
        self.fail_read_after_write = fail_read_after_write
        self.read_failed_once = False
    def is_enabled(self):
        self.calls.append(("read",))
        if self.fail_read_after_write and self.read_failed_once:
            raise RuntimeError("read denied")
        return self.enabled
    def set_enabled(self, enabled, *, start_minimized=True):
        self.calls.append(("write", enabled, start_minimized)); self.enabled = enabled
        self.minimized = start_minimized
        if self.fail_write:
            self.read_failed_once = True
            raise RuntimeError("verification failed")


class DesktopWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, directory):
        return DesktopWindow(Path(directory), safe_mode=True, router_factory=lambda *_args: FakeRouter(),
                             feedback_factory=FakeFeedback, tray_available=False)

    def test_pair_selection_is_distinct_and_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "", "", ""), None)
            self.assertEqual(window.desktop_combo.currentData(), "system-mic")
            self.assertEqual(window.headset_combo.currentData(), "nova-mic")
            window.headset_combo.setCurrentIndex(window.headset_combo.findData("usb-mic"))
            self.assertTrue(window.start_button.isEnabled())
            self.assertEqual(window.settings.desktop_source, "system-mic")
            self.assertEqual(window.settings.headset_source, "usb-mic")
            window.headset_combo.setCurrentIndex(window.headset_combo.findData("system-mic"))
            self.assertFalse(window.start_button.isEnabled())
            window.close()

    def test_safe_mode_does_not_start_devices_or_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            FakeFeedback.output_calls = 0
            FakeFeedback.instances.clear()
            window = self.make_window(directory)
            self.assertEqual(window.status.text(), "Safe mode · no devices opened")
            self.assertIsNone(window.feedback)
            self.assertFalse((Path(directory) / "settings.json").exists())
            self.assertFalse(window.start_button.isEnabled())
            self.assertEqual(FakeFeedback.output_calls, 0)
            self.assertEqual(FakeFeedback.instances, [])
            window.close()

    def test_scope_choices_are_visible_and_linux_defaults_to_discord(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            if window.scope_combo.count() > 1:
                self.assertEqual(window.scope_combo.currentData(), "discord")
                self.assertEqual(window.scope_combo.itemData(1), "system")
            window.close()

    def test_nova_headset_suggestion_and_generic_desktop_default(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "", "", ""), None)
            self.assertEqual(window.desktop_combo.currentData(), "system-mic")
            self.assertEqual(window.headset_combo.currentData(), "nova-mic")
            self.assertTrue(window.start_button.isEnabled())
            window.close()

    def test_unique_nova_output_is_suggested_and_saved_without_default_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            FakeFeedback.all_outputs = [FakeOutput("Nova 5P headset output", b"nova-out")]
            window = self.make_window(directory)
            window.safe_mode = False
            window.feedback = FakeFeedback(directory, output_id="")
            window.refresh_outputs()
            self.assertEqual(window.output_combo.currentData(), "bm92YS1vdXQ=")
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "", "", ""), None)
            self.assertEqual(window.settings.feedback_output_id, "bm92YS1vdXQ=")
            FakeFeedback.all_outputs = []
            window.close()

    def test_discord_without_active_call_still_allows_microphone_pair_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            if window.scope_combo.currentData() != "discord":
                self.skipTest("Discord-specific capture routing is available only in Discord scope")
            window._sources_loaded(window._source_generation,
                                   (FakeRouter().list_sources(), "", "", "", "no Discord recording stream"), None)
            self.assertEqual(window.desktop_combo.currentData(), "system-mic")
            self.assertEqual(window.headset_combo.currentData(), "nova-mic")
            self.assertIn("start or join a voice call", window.detail.text())
            window.close()

    def test_unavailable_saved_microphone_is_retained_and_cannot_start(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="lost-id", headset_source="usb-mic", auto_start=False))
            window = self.make_window(directory)
            window.safe_mode = False
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "lost-id", "usb-mic", ""), None)
            self.assertEqual(window.desktop_combo.currentData(), "lost-id")
            self.assertIn("unavailable", window.desktop_combo.currentText())
            self.assertFalse(window.start_button.isEnabled())
            window.close()

    def test_stale_async_source_result_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window._source_generation = 9
            window._sources_loaded(8, ([AudioSource("old", "Old")], "old", "", ""), None)
            self.assertEqual(window.sources, [])
            window.close()

    def test_first_run_autoselection_does_not_consent_to_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            self.assertTrue(window.resume_check.isChecked())
            window.safe_mode = False
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "", "", ""), None)
            window.resume_check.setChecked(True)
            self.assertFalse(window.settings.auto_start)
            window.close()

    def test_startup_read_is_reflected_not_inferred_from_saved_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = FakeStartup(enabled=True)
            window = self.make_window(directory)
            window.safe_mode = False
            window.startup_manager = startup
            window._startup_busy = True
            window._startup_result(True, None)
            self.assertTrue(window.login_check.isChecked())
            self.assertEqual(startup.calls, [])
            window.close()

    def test_startup_mutation_is_only_explicit_and_general_save_does_not_register(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = FakeStartup()
            window = self.make_window(directory)
            window.safe_mode = False
            window.startup_manager = startup
            window._startup_actual = False
            window._startup_state_known = True
            class InlinePool:
                def start(self, task): task.run()
            window.pool = InlinePool()
            window._save_settings()
            self.assertEqual(startup.calls, [])
            window._change_startup(True, True)
            self.assertEqual(startup.calls, [("write", True, True), ("read",)])
            self.assertTrue(window.login_check.isChecked())
            window.close()

    def test_enabled_startup_persists_minimized_choice_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", start_at_login=True, start_minimized=True))
            startup = FakeStartup(enabled=True)
            window = self.make_window(directory)
            window.settings = AppSettings(desktop_source="system-mic", headset_source="nova-mic", start_at_login=True, start_minimized=True)
            window.safe_mode = False
            window.startup_manager = startup
            window._startup_actual = True
            window._startup_state_known = True
            class InlinePool:
                def start(self, task): task.run()
            window.pool = InlinePool()
            window.minimized_check.setChecked(False)
            persisted = __import__("nova_mic_switch.settings", fromlist=["load_settings"]).load_settings(Path(directory))
            self.assertEqual(startup.calls, [("write", True, False), ("read",)])
            self.assertTrue(persisted.start_at_login)
            self.assertFalse(persisted.start_minimized)
            window.close()

    def test_disabled_login_minimized_choice_saves_without_native_write(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(start_at_login=False))
            startup = FakeStartup(enabled=False)
            window = self.make_window(directory)
            window.safe_mode = False
            window.startup_manager = startup
            window._startup_actual = False
            window._startup_state_known = True
            class InlinePool:
                def start(self, task): task.run()
            window.pool = InlinePool()
            window.minimized_check.setChecked(False)
            window._minimized_toggled(False)
            from nova_mic_switch.settings import load_settings
            self.assertFalse(load_settings(Path(directory)).start_minimized)
            self.assertEqual(startup.calls, [])
            window.close()

    def test_partial_startup_write_refreshes_and_shows_actual_state(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = FakeStartup(enabled=False, fail_write=True)
            window = self.make_window(directory)
            window.safe_mode = False
            window.startup_manager = startup
            window._startup_actual = False
            window._startup_state_known = True
            class InlinePool:
                def start(self, task): task.run()
            window.pool = InlinePool()
            window._change_startup(True, False)
            self.assertTrue(window.login_check.isChecked())
            self.assertIn("Could not change login startup", window.detail.text())
            self.assertTrue(window.settings.start_minimized)
            window.close()

    def test_partial_startup_write_with_failed_refresh_marks_state_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = FakeStartup(enabled=False, fail_write=True, fail_read_after_write=True)
            window = self.make_window(directory)
            window.safe_mode = False
            window.startup_manager = startup
            window._startup_actual = False
            window._startup_state_known = True
            class InlinePool:
                def start(self, task): task.run()
            window.pool = InlinePool()
            window.login_check.blockSignals(True)
            window.login_check.setChecked(False)
            window.login_check.blockSignals(False)
            window._change_startup(True, False)
            self.assertTrue(window.login_check.isTristate())
            self.assertEqual(window.login_check.checkState().name, "PartiallyChecked")
            self.assertFalse(window.login_check.isEnabled())
            self.assertIn("state is unknown", window.detail.text())
            self.assertEqual(startup.calls, [("write", True, False), ("read",)])
            window.login_check.setChecked(True)
            window._save_settings()
            self.assertEqual(startup.calls, [("write", True, False), ("read",)])
            window.close()

    def test_startup_query_failure_does_not_present_saved_preference_as_actual(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = FakeStartup(enabled=False)
            window = self.make_window(directory)
            window.safe_mode = False
            window.startup_manager = startup
            class InlinePool:
                def start(self, task): task.run()
            window.pool = InlinePool()
            window.login_check.blockSignals(True)
            window.login_check.setChecked(False)
            window.login_check.blockSignals(False)
            window._startup_busy = True
            window._startup_result(None, "access denied")
            self.assertEqual(window.login_check.checkState().name, "PartiallyChecked")
            self.assertFalse(window.login_check.isEnabled())
            self.assertEqual(startup.calls, [])
            window.login_check.setChecked(True)
            window._save_settings()
            self.assertEqual(startup.calls, [])
            window.close()

    def test_saved_pair_autostarts_even_when_initial_audio_enumeration_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=True))
            window = DesktopWindow(Path(directory), safe_mode=True, service_factory=FakeService,
                                   router_factory=lambda *_args: FakeRouter(), feedback_factory=FakeFeedback,
                                   tray_available=False)
            window.safe_mode = False
            window._autostart_pending = True
            window._sources_loaded(window._source_generation, None, "audio not ready")
            self.assertIsNotNone(window.service)
            self.assertIn("Reconnecting", window.status.text())
            window.service.running = False
            window.service = None
            window.close()

    def test_missing_saved_device_is_not_treated_as_available_but_resume_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=True))
            window = DesktopWindow(Path(directory), safe_mode=True, service_factory=FakeService,
                                   router_factory=lambda *_args: FakeRouter(), feedback_factory=FakeFeedback,
                                   tray_available=False)
            window.safe_mode = False
            window._sources_loaded(window._source_generation, ([AudioSource("system-mic", "Computer", True)], "", "system-mic", "nova-mic", ""), None)
            self.assertFalse(window._pair_is_available())
            window._autostart_pending = True
            window._sources_loaded(window._source_generation, ([AudioSource("system-mic", "Computer", True)], "", "system-mic", "nova-mic", ""), None)
            self.assertIsNotNone(window.service)
            window.service.running = False
            window.service = None
            window.close()

    def test_explicit_confirm_persists_resume_choice_and_autostart_is_noninteractive(self):
        from PySide6.QtWidgets import QMessageBox
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=False))
            window = DesktopWindow(Path(directory), safe_mode=True, service_factory=FakeService,
                                  router_factory=lambda *_args: FakeRouter(), feedback_factory=FakeFeedback,
                                  tray_available=False)
            window.safe_mode = False
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "system-mic", "nova-mic", ""), None)
            window.resume_check.setChecked(True)
            self.assertFalse(window.settings.auto_start)
            with patch("nova_mic_switch.desktop.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window.toggle_service()
            self.assertTrue(window.settings.auto_start)
            self.assertIsNotNone(window.service)
            service = window.service
            service.running = False
            window.service = None
            window.close()

            # An opted-in profile starts after its async source validation without a modal prompt.
            second = DesktopWindow(Path(directory), safe_mode=True, service_factory=FakeService,
                                   router_factory=lambda *_args: FakeRouter(), feedback_factory=FakeFeedback,
                                   tray_available=False)
            second.safe_mode = False
            second._autostart_pending = True
            second._sources_loaded(second._source_generation, (FakeRouter().list_sources(), "system-mic", "system-mic", "nova-mic", ""), None)
            self.assertIsNotNone(second.service)
            second.service = None
            second.close()

    def test_editing_a_confirmed_pair_clears_resume_consent_until_next_start(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=True))
            window = DesktopWindow(Path(directory), safe_mode=True, router_factory=lambda *_args: FakeRouter(),
                                  feedback_factory=FakeFeedback, tray_available=False)
            window.safe_mode = False
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "system-mic", "nova-mic", ""), None)
            self.assertTrue(window.settings.auto_start)
            window.headset_combo.setCurrentIndex(window.headset_combo.findData("usb-mic"))
            self.assertFalse(window.settings.auto_start)
            self.assertFalse(window.resume_check.isChecked())
            window.close()

    def test_corrupt_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("not json", encoding="utf-8")
            window = self.make_window(directory)
            self.assertTrue(window.load_error)
            self.assertFalse(window.reset_button.isHidden())
            self.assertFalse(window.start_button.isEnabled())
            self.assertEqual(path.read_text(encoding="utf-8"), "not json")
            window.close()

    def test_route_feedback_requires_applied_verified_pair_target(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window.feedback = FakeFeedback()
            window.settings = AppSettings(desktop_source="system-mic", headset_source="usb-mic")
            for event in (
                {"type": "switch", "applied": False, "dry_run": True, "target_source": "usb-mic"},
                {"type": "switch", "applied": True, "dry_run": False, "target_source": "unknown"},
            ):
                window._service_event(event)
            self.assertEqual(window.feedback.played, [])
            window._service_event({"type": "switch", "applied": True, "dry_run": False, "target_source": "usb-mic"})
            self.assertEqual(window.feedback.played[0][0], "route")
            self.assertEqual(window.status.text(), "Headset microphone active")
            window.close()

    def test_running_configuration_is_locked_and_failed_stop_keeps_service(self):
        with tempfile.TemporaryDirectory() as directory:
            FakeService.instances.clear()
            window = DesktopWindow(Path(directory), safe_mode=True, service_factory=FakeService,
                                  router_factory=lambda *_args: FakeRouter(), feedback_factory=FakeFeedback,
                                  tray_available=False)
            window.safe_mode = False
            window.feedback = FakeFeedback()
            window._sources_loaded(window._source_generation, (FakeRouter().list_sources(), "system-mic", "", "", ""), None)
            window.settings = AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=False)
            with patch("nova_mic_switch.desktop.QMessageBox.question", return_value=__import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton.Yes):
                window.toggle_service()
            service = window.service
            self.assertIsNotNone(service)
            self.assertFalse(window.scope_combo.isEnabled())
            window.toggle_service()
            self.assertIs(window.service, service)
            self.assertEqual(service.stop_calls, 1)
            self.assertEqual(len(FakeService.instances), 1)
            service.running = False
            window._poll_service_stop()
            self.assertIsNone(window.service)
            self.assertEqual(window.start_button.text(), "Start listening")
            window._closing = False
            window.close()

    def test_missing_saved_sound_output_remains_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(feedback_output_id="bm92YQ==", auto_start=False))
            window = DesktopWindow(Path(directory), safe_mode=True, router_factory=lambda *_args: FakeRouter(),
                                  feedback_factory=FakeFeedback, tray_available=False)
            window.safe_mode = False
            window.feedback = FakeFeedback()
            window.refresh_outputs()
            self.assertEqual(window.output_combo.currentData(), "bm92YQ==")
            self.assertIn("unavailable", window.output_combo.currentText())
            self.assertEqual(window.settings.feedback_output_id, "bm92YQ==")
            window.close()

    def test_failed_settings_save_is_visible_and_disables_start(self):
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            with patch("nova_mic_switch.desktop.save_settings", side_effect=OSError("read-only")):
                window._save_settings()
            self.assertIn("not saved", window.detail.text())
            self.assertFalse(window.start_button.isEnabled())
            window.close()

    def test_single_instance_manual_activates_but_tray_request_does_not_focus(self):
        from nova_mic_switch.desktop import InstanceCoordinator
        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime_dir = Path(directory) / "runtime"
            runtime_dir.mkdir(mode=0o700)
            long_tmp = Path(directory) / ("long-tmp-" + "x" * 60)
            other_tmp = Path(directory) / ("other-tmp-" + "y" * 40)
            long_tmp.mkdir()
            other_tmp.mkdir()
            config_dir = Path(directory) / "config"
            primary = None
            try:
                with patch.dict(os.environ, {"TMPDIR": str(long_tmp), "XDG_RUNTIME_DIR": str(runtime_dir)}):
                    primary = InstanceCoordinator(config_dir)
                    if sys.platform.startswith("linux"):
                        self.assertTrue(primary.server_name.startswith(str(runtime_dir) + os.sep))
                        self.assertLessEqual(len(os.fsencode(primary.server_name)), 106)
                    self.assertTrue(primary.acquire_or_notify("activate"))
                    socket_path = None
                    if sys.platform.startswith("linux"):
                        self.assertEqual(primary.server.fullServerName(), primary.server_name)
                        socket_path = Path(primary.server.fullServerName())
                        self.assertTrue(socket_path.exists())

                from PySide6.QtTest import QSignalSpy
                spy = QSignalSpy(primary.activation)
                connection_spy = QSignalSpy(primary.server.newConnection)
                def notify(command):
                    child_script = """
import json, sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from nova_mic_switch.desktop import InstanceCoordinator

app = QCoreApplication([])
coordinator = InstanceCoordinator(Path(sys.argv[1]))
try:
    result = coordinator.acquire_or_notify(sys.argv[2])
    print(json.dumps({"result": result}), flush=True)
finally:
    coordinator.close()
"""
                    child_env = os.environ.copy()
                    child_env.update({
                        "TMPDIR": str(other_tmp),
                        "XDG_RUNTIME_DIR": str(runtime_dir),
                        "QT_QPA_PLATFORM": "offscreen",
                    })
                    child = subprocess.Popen(
                        [sys.executable, "-c", child_script, str(config_dir), command],
                        cwd=str(Path(__file__).resolve().parents[1]),
                        env=child_env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    from PySide6.QtCore import QElapsedTimer
                    timer = QElapsedTimer()
                    timer.start()
                    try:
                        while child.poll() is None and timer.elapsed() < 5000:
                            self.app.processEvents()
                        timed_out = child.poll() is None
                    finally:
                        if child.poll() is None:
                            child.kill()
                        stdout, stderr = child.communicate(timeout=2)
                    connection_state = []
                    for socket, buffer in primary._connection_buffers.items():
                        try:
                            state = socket.state()
                            available = socket.bytesAvailable()
                        except RuntimeError as exc:
                            state, available = f"unavailable: {exc}", "unavailable"
                        connection_state.append((len(buffer) if buffer is not None else None, state, available))
                    diagnostics = (
                        f"command={command!r}, timed_out={timed_out}, exit={child.returncode}, "
                        f"stdout={stdout!r}, stderr={stderr!r}, listening={primary.server.isListening()}, "
                        f"pending={primary.server.hasPendingConnections()}, new_connections={connection_spy.count()}, "
                        f"accepted_connections={len(primary._connection_buffers)}, activations={spy.count()}, "
                        f"tracked_connections={connection_state}"
                    )
                    self.assertFalse(timed_out, diagnostics)
                    self.assertEqual(child.returncode, 0, diagnostics)
                    try:
                        payload = json.loads(stdout)
                    except json.JSONDecodeError as exc:
                        self.fail(f"Child did not return valid JSON: {exc}; {diagnostics}")
                    self.assertEqual(payload, {"result": False}, diagnostics)
                    self.assertTrue(primary.server.isListening(), diagnostics)

                notify("activate")
                self.assertTrue(primary.server.isListening())
                if socket_path is not None:
                    self.assertTrue(socket_path.exists(), "failed lock acquisition must not unlink the active endpoint")
                self.assertEqual(spy.count(), 1)
                self.assertEqual(spy.at(0), ["activate"])
                notify("tray")
                self.assertTrue(primary.server.isListening())
                self.assertEqual(spy.count(), 2)
                self.assertEqual(spy.at(1), ["tray"])
            finally:
                if primary is not None:
                    primary.close()

    def test_single_instance_client_accepts_fragmented_ack_after_flushed_write(self):
        from PySide6.QtNetwork import QLocalSocket
        from nova_mic_switch.desktop import InstanceCoordinator

        class FakeSocket:
            def __init__(self):
                self.connected = False
                self.response = []
                self.writes = []
                self.connect_count = 0
                self.flush_waits = 0

            def connectToServer(self, _name):
                self.connect_count += 1

            def waitForConnected(self, _timeout):
                self.connected = True
                return True

            def write(self, data):
                self.writes.append(data)
                return len(data)

            def flush(self):
                return True

            def bytesToWrite(self):
                return 1 if self.flush_waits == 0 else 0

            def waitForBytesWritten(self, _timeout):
                self.flush_waits += 1
                return False

            def bytesAvailable(self):
                return len(self.response[0]) if self.response else 0

            def read(self, _size):
                return self.response.pop(0)

            def waitForReadyRead(self, _timeout):
                if not self.response:
                    self.response.extend((b"O", b"K\n"))
                return True

            def state(self):
                return QLocalSocket.LocalSocketState.ConnectedState

            def abort(self):
                self.connected = False

            def deleteLater(self):
                pass

        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            socket = FakeSocket()
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                coordinator = InstanceCoordinator(Path(directory) / "config")
                with patch.object(coordinator.lock, "tryLock", return_value=False), \
                     patch("nova_mic_switch.desktop.QLocalSocket", return_value=socket):
                    self.assertFalse(coordinator.acquire_or_notify("activate"))
                self.assertEqual(socket.writes, [b"activate\n"])
                self.assertEqual(socket.connect_count, 1)
                self.assertEqual(socket.flush_waits, 1)

    def test_single_instance_client_rejects_invalid_or_missing_ack_without_resending(self):
        from PySide6.QtNetwork import QLocalSocket
        from nova_mic_switch.desktop import InstanceCoordinator

        class FakeSocket:
            def __init__(self, response):
                self.response = response
                self.writes = []
                self.connect_count = 0

            def connectToServer(self, _name):
                self.connect_count += 1

            def waitForConnected(self, _timeout):
                return True

            def write(self, data):
                self.writes.append(data)
                return len(data)

            def flush(self):
                return True

            def bytesToWrite(self):
                return 0

            def bytesAvailable(self):
                return len(self.response)

            def read(self, _size):
                data, self.response = self.response, b""
                return data

            def waitForReadyRead(self, _timeout):
                return False

            def state(self):
                return QLocalSocket.LocalSocketState.ConnectedState

            def abort(self):
                pass

            def deleteLater(self):
                pass

        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                coordinator = InstanceCoordinator(Path(directory) / "config")
                for response, expected in ((b"NO\n", "invalid acknowledgement"), (b"", "Timed out")):
                    socket = FakeSocket(response)
                    tick = [0.0]

                    def monotonic_tick():
                        tick[0] += 0.5
                        return tick[0]

                    with self.subTest(response=response), \
                         patch.object(coordinator.lock, "tryLock", return_value=False), \
                         patch("nova_mic_switch.desktop.QLocalSocket", return_value=socket), \
                         patch("nova_mic_switch.desktop.time.monotonic", side_effect=monotonic_tick):
                        with self.assertRaisesRegex(RuntimeError, expected):
                            coordinator.acquire_or_notify("tray")
                        self.assertEqual(socket.writes, [b"tray\n"])
                        self.assertEqual(socket.connect_count, 1)

    def test_single_instance_client_backs_off_failed_connects_within_deadline(self):
        from nova_mic_switch.desktop import InstanceCoordinator

        class FakeSocket:
            def __init__(self, clock):
                self.clock = clock
                self.connect_count = 0

            def connectToServer(self, _name):
                self.connect_count += 1

            def waitForConnected(self, timeout):
                self.clock[0] += min(timeout / 1000, 0.1)
                return False

            def abort(self):
                pass

            def deleteLater(self):
                pass

        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            clock = [0.0]
            sleeps = []

            def fake_sleep(duration):
                sleeps.append(duration)
                clock[0] += duration

            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                coordinator = InstanceCoordinator(Path(directory) / "config")
                socket = FakeSocket(clock)
                with patch.object(coordinator.lock, "tryLock", return_value=False), \
                     patch("nova_mic_switch.desktop.QLocalSocket", return_value=socket), \
                     patch("nova_mic_switch.desktop.time.monotonic", side_effect=lambda: clock[0]), \
                     patch("nova_mic_switch.desktop.time.sleep", side_effect=fake_sleep):
                    with self.assertRaisesRegex(RuntimeError, "Could not contact the existing"):
                        coordinator.acquire_or_notify("activate")
                self.assertGreater(socket.connect_count, 1)
                self.assertLessEqual(socket.connect_count, 30)
                self.assertTrue(sleeps)
                self.assertTrue(all(0 < delay <= 0.02 for delay in sleeps))
                self.assertLessEqual(clock[0], coordinator._IPC_DEADLINE_SECONDS)

    def test_single_instance_client_fails_if_disconnected_with_pending_write(self):
        from PySide6.QtNetwork import QLocalSocket
        from nova_mic_switch.desktop import InstanceCoordinator

        class FakeSocket:
            def __init__(self):
                self.writes = []
                self.connect_count = 0
                self.write_waits = 0

            def connectToServer(self, _name):
                self.connect_count += 1

            def waitForConnected(self, _timeout):
                return True

            def write(self, data):
                self.writes.append(data)
                return len(data)

            def flush(self):
                return True

            def bytesToWrite(self):
                return 1

            def waitForBytesWritten(self, _timeout):
                self.write_waits += 1
                return False

            def state(self):
                return QLocalSocket.LocalSocketState.UnconnectedState

            def abort(self):
                pass

            def deleteLater(self):
                pass

        class SocketFactory:
            LocalSocketState = QLocalSocket.LocalSocketState

            def __new__(cls):
                return socket

        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                coordinator = InstanceCoordinator(Path(directory) / "config")
                socket = FakeSocket()
                with patch.object(coordinator.lock, "tryLock", return_value=False), \
                     patch("nova_mic_switch.desktop.QLocalSocket", new=SocketFactory):
                    with self.assertRaisesRegex(RuntimeError, "disconnected while the command was being sent"):
                        coordinator.acquire_or_notify("tray")
                self.assertEqual(socket.connect_count, 1)
                self.assertEqual(socket.writes, [b"tray\n"])
                self.assertEqual(socket.write_waits, 0)

    def test_single_instance_connection_cleanup_is_explicit_and_idempotent(self):
        from PySide6.QtNetwork import QLocalSocket
        from nova_mic_switch.desktop import InstanceCoordinator

        class FakeSocket:
            def __init__(self, state):
                self.socket_state = state
                self.aborts = 0
                self.deletions = 0

            def state(self):
                return self.socket_state

            def abort(self):
                self.aborts += 1
                self.socket_state = QLocalSocket.LocalSocketState.UnconnectedState

            def deleteLater(self):
                self.deletions += 1

        class FakeTimer:
            def __init__(self):
                self.stops = 0

            def stop(self):
                self.stops += 1

        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                coordinator = InstanceCoordinator(Path(directory) / "config")
                disconnected = FakeSocket(QLocalSocket.LocalSocketState.UnconnectedState)
                disconnected_timer = FakeTimer()
                coordinator._connection_buffers[disconnected] = bytearray()
                coordinator._connection_timers[disconnected] = disconnected_timer
                coordinator._discard_connection(disconnected)
                self.assertEqual((disconnected.aborts, disconnected.deletions, disconnected_timer.stops), (0, 1, 1))

                expired = FakeSocket(QLocalSocket.LocalSocketState.ConnectedState)
                expired_timer = FakeTimer()
                coordinator._connection_buffers[expired] = bytearray()
                coordinator._connection_timers[expired] = expired_timer
                coordinator._discard_connection(expired, abort=True)
                coordinator._discard_connection(expired, abort=True)
                self.assertEqual((expired.aborts, expired.deletions, expired_timer.stops), (1, 1, 1))

                closing = FakeSocket(QLocalSocket.LocalSocketState.ConnectedState)
                closing_timer = FakeTimer()
                coordinator._connection_buffers[closing] = bytearray()
                coordinator._connection_timers[closing] = closing_timer
                coordinator.primary = True
                with patch.object(coordinator.lock, "unlock"):
                    coordinator.close()
                self.assertEqual((closing.aborts, closing.deletions, closing_timer.stops), (1, 1, 1))
                self.assertEqual(coordinator._connection_buffers, {})
                self.assertEqual(coordinator._connection_timers, {})

    def test_single_instance_server_buffers_fragmented_frames_once(self):
        from PySide6.QtTest import QSignalSpy
        from nova_mic_switch.desktop import InstanceCoordinator

        class FakeSocket:
            def __init__(self):
                self.chunks = []
                self.acks = []
                self.aborted = False

            def readAll(self):
                return self.chunks.pop(0) if self.chunks else b""

            def bytesAvailable(self):
                return bool(self.chunks)

            def write(self, data):
                self.acks.append(data)
                return len(data)

            def flush(self):
                return True

            def abort(self):
                self.aborted = True

        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                coordinator = InstanceCoordinator(Path(directory) / "config")
                spy = QSignalSpy(coordinator.activation)
                socket = FakeSocket()
                coordinator._connection_buffers[socket] = bytearray()
                socket.chunks.append(b"act")
                coordinator._read_connection(socket)
                self.assertEqual(spy.count(), 0)
                socket.chunks.append(b"ivate\n")
                coordinator._read_connection(socket)
                coordinator._read_connection(socket)
                self.assertEqual(spy.count(), 1)
                self.assertEqual(spy.at(0), ["activate"])
                self.assertEqual(socket.acks, [b"OK\n"])

    def test_single_instance_profiles_have_distinct_runtime_endpoints(self):
        from nova_mic_switch.desktop import InstanceCoordinator
        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            runtime_dir = Path(directory) / "runtime"
            runtime_dir.mkdir(mode=0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime_dir)}):
                first = InstanceCoordinator(Path(directory) / "config one")
                second = InstanceCoordinator(Path(directory) / "config two")
                self.assertNotEqual(first.server_name, second.server_name)
                if sys.platform.startswith("linux"):
                    self.assertTrue(first.server_name.startswith(str(runtime_dir) + os.sep))
                    self.assertTrue(second.server_name.startswith(str(runtime_dir) + os.sep))
                try:
                    self.assertTrue(first.acquire_or_notify("activate"))
                    self.assertTrue(second.acquire_or_notify("activate"))
                finally:
                    first.close()
                    second.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux runtime-directory validation")
    def test_single_instance_rejects_untrusted_or_overlong_runtime_location(self):
        from nova_mic_switch.desktop import InstanceCoordinator
        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(dir=fixture_root) as directory:
            root = Path(directory)
            insecure = root / "insecure-runtime"
            insecure.mkdir(mode=0o755)
            insecure.chmod(0o755)
            overlong = root / ("runtime-" + "z" * 90)
            overlong.mkdir(mode=0o700)
            with patch("nova_mic_switch.desktop.QStandardPaths.writableLocation", return_value=str(insecure)):
                with self.assertRaisesRegex(RuntimeError, "not be accessible by other users"):
                    InstanceCoordinator(root / "insecure-config")
            with patch("nova_mic_switch.desktop.QStandardPaths.writableLocation", return_value=str(overlong)):
                with self.assertRaisesRegex(RuntimeError, "too long for a local activation socket"):
                    InstanceCoordinator(root / "overlong-config")
            self.assertFalse((root / "insecure-config/.nova-mic-switch.lock").exists())
            self.assertFalse((root / "overlong-config/.nova-mic-switch.lock").exists())

    def test_tray_unavailable_launch_is_visible_and_actionable(self):
        from nova_mic_switch.desktop import present_window
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            present_window(window, tray_requested=True)
            self.assertTrue(window.isVisible())
            self.assertIn("tray is unavailable", window.detail.text())
            window.close()

    def test_tray_first_run_stays_hidden_and_shows_setup_notice(self):
        from types import SimpleNamespace
        from nova_mic_switch.desktop import present_window
        class Tray:
            def __init__(self): self.notice = None
            def showMessage(self, *args): self.notice = args
        tray = Tray()
        window = SimpleNamespace(tray_available=True, settings=AppSettings(), tray=tray,
                                 hidden=False, shown=False)
        window.hide = lambda: setattr(window, "hidden", True)
        window.show = lambda: setattr(window, "shown", True)
        present_window(window, tray_requested=True)
        self.assertTrue(window.hidden)
        self.assertFalse(window.shown)
        self.assertIn("Setup needed", tray.notice[0])

    def test_confirmed_normal_launch_respects_minimized_preference(self):
        from nova_mic_switch.desktop import present_window
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=True, start_minimized=True))
            window = self.make_window(directory)
            window.safe_mode = False
            window.config_existed = True
            window.tray_available = True
            window.hidden = False
            window.hide = lambda: setattr(window, "hidden", True)
            window.show = lambda: setattr(window, "hidden", False)
            present_window(window, tray_requested=False)
            self.assertTrue(window.hidden)
            window.close()

    def test_first_normal_launch_with_setup_remains_visible(self):
        from nova_mic_switch.desktop import present_window
        with tempfile.TemporaryDirectory() as directory:
            window = self.make_window(directory)
            window.safe_mode = False
            window.config_existed = False
            window.tray_available = True
            window.hidden = False
            window.hide = lambda: setattr(window, "hidden", True)
            window.show = lambda: setattr(window, "hidden", False)
            present_window(window, tray_requested=False)
            self.assertFalse(window.hidden)
            window.close()

    def test_false_minimized_preference_first_run_and_safe_mode_stay_visible(self):
        from nova_mic_switch.desktop import present_window
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=True, start_minimized=False))
            window = self.make_window(directory)
            window.config_existed = True
            window.tray_available = True
            window.hidden = False
            window.hide = lambda: setattr(window, "hidden", True)
            window.show = lambda: setattr(window, "hidden", False)
            window.safe_mode = False
            present_window(window, tray_requested=False)
            self.assertFalse(window.hidden)
            window.safe_mode = True
            present_window(window, tray_requested=True)
            self.assertFalse(window.hidden)
            window.close()

    def test_no_tray_always_keeps_window_visible_with_fallback_message(self):
        from nova_mic_switch.desktop import present_window
        with tempfile.TemporaryDirectory() as directory:
            save_settings(Path(directory), AppSettings(desktop_source="system-mic", headset_source="nova-mic", auto_start=True, start_minimized=True))
            window = self.make_window(directory)
            window.config_existed = True
            window.safe_mode = False
            present_window(window, tray_requested=False)
            self.assertTrue(window.isVisible())
            self.assertIn("tray is unavailable", window.detail.text())
            window.close()


if __name__ == "__main__":
    unittest.main()
