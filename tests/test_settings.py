import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nova_mic_switch.settings import (
    AppSettings,
    SETTINGS_VERSION,
    SettingsError,
    load_settings,
    save_settings,
    validate_settings,
)


class SettingsTests(unittest.TestCase):
    def test_first_run_defaults_are_incomplete_and_valid_without_sources(self):
        settings = AppSettings()
        self.assertEqual(settings.desktop_source, "")
        self.assertEqual(settings.headset_source, "")
        self.assertEqual(settings.trigger, "previous-track")
        self.assertTrue(settings.feedback_enabled)
        self.assertEqual(settings.feedback_volume, 0.25)
        self.assertTrue(settings.start_minimized)
        self.assertTrue(settings.auto_start)
        validate_settings(settings)
        with self.assertRaisesRegex(SettingsError, "both be selected"):
            validate_settings(settings, require_sources=True)

    def test_platform_defaults_for_scope_and_login(self):
        with patch("nova_mic_switch.settings.sys.platform", "linux"):
            linux = AppSettings()
        self.assertEqual(linux.scope, "discord")
        self.assertFalse(linux.start_at_login)

        with patch("nova_mic_switch.settings.sys.platform", "win32"):
            windows = AppSettings()
        self.assertEqual(windows.scope, "system")
        self.assertTrue(windows.start_at_login)

    def test_strict_types_trigger_scope_and_volume_validation(self):
        invalid = (
            AppSettings(desktop_source=1),
            AppSettings(trigger="volume-up"),
            AppSettings(scope="global"),
            AppSettings(feedback_enabled=1),
            AppSettings(feedback_output_id=None),
            AppSettings(feedback_volume=float("nan")),
            AppSettings(feedback_volume=float("inf")),
            AppSettings(feedback_volume=-0.01),
            AppSettings(feedback_volume=1.01),
            AppSettings(start_minimized="yes"),
        )
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(SettingsError):
                validate_settings(settings)
        self.assertTrue(math.isfinite(AppSettings().feedback_volume))

    def test_require_sources_rejects_same_or_missing_source_ids(self):
        for settings in (AppSettings(), AppSettings(desktop_source="mic", headset_source=""),
                         AppSettings(desktop_source="mic", headset_source="mic")):
            with self.subTest(settings=settings), self.assertRaises(SettingsError):
                validate_settings(settings, require_sources=True)

    def test_windows_rejects_discord_scope(self):
        with patch("nova_mic_switch.settings.sys.platform", "win32"):
            with self.assertRaisesRegex(SettingsError, "invalid for this platform"):
                validate_settings(AppSettings(scope="discord"))

    def test_absent_config_returns_defaults_and_save_load_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = load_settings(root)
            self.assertEqual(defaults, AppSettings())
            chosen = AppSettings(
                desktop_source="desk-id",
                headset_source="headset-id",
                trigger="next-track",
                scope="system",
                feedback_enabled=False,
                feedback_output_id="opaque-qt-device-id",
                feedback_volume=0.5,
                start_at_login=True,
                start_minimized=False,
                auto_start=False,
            )
            save_settings(root, chosen)
            doc = json.loads((root / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], SETTINGS_VERSION)
            self.assertEqual(load_settings(root), chosen)

    def test_unknown_future_settings_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "version": SETTINGS_VERSION,
                "settings": {"trigger": "play-pause", "future_option": {"x": 1}},
                "future_root_key": True,
            }), encoding="utf-8")
            loaded = load_settings(Path(directory))
            self.assertEqual(loaded.trigger, "play-pause")
            self.assertEqual(loaded.desktop_source, "")

    def test_malformed_or_future_version_is_rejected_without_modifying_file(self):
        for content in ("{broken", json.dumps({"version": 2, "settings": {}}),
                        json.dumps({"version": SETTINGS_VERSION, "settings": []})):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(SettingsError):
                    load_settings(Path(directory))
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_failed_atomic_replace_preserves_old_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            original = '{"preserve":"old"}\n'
            path.write_text(original, encoding="utf-8")
            with patch("nova_mic_switch.settings.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(SettingsError, "cannot save settings"):
                    save_settings(root, AppSettings(trigger="play-pause"))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(root.iterdir()), [path])

    def test_invalid_save_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = "leave unchanged"
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(SettingsError):
                save_settings(Path(directory), AppSettings(feedback_volume=float("nan")))
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
