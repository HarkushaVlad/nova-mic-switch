import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nova_mic_switch.startup import (
    LINUX_FILENAME,
    WINDOWS_RUN_KEY,
    WINDOWS_VALUE_NAME,
    StartupError,
    StartupManager,
    default_startup_command,
)


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.keys = {}
        self.calls = []
        self.fail_set = None
        self.wrong_readback = False

    def OpenKey(self, root, path, reserved, access):
        self.calls.append(("open", root, path, access))
        if path not in self.keys:
            raise FileNotFoundError(path)
        return path

    def CreateKeyEx(self, root, path, reserved, access):
        self.calls.append(("create", root, path, access))
        self.keys.setdefault(path, {})
        return path

    def QueryValueEx(self, key, name):
        self.calls.append(("query", key, name))
        if name not in self.keys[key]:
            raise FileNotFoundError(name)
        value, value_type = self.keys[key][name]
        return ("other" if self.wrong_readback else value, value_type)

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.calls.append(("set", key, name, value_type, value))
        if self.fail_set:
            raise self.fail_set
        self.keys[key][name] = (value, value_type)

    def DeleteValue(self, key, name):
        self.calls.append(("delete", key, name))
        if name not in self.keys[key]:
            raise FileNotFoundError(name)
        del self.keys[key][name]

    def CloseKey(self, key):
        self.calls.append(("close", key))


class StartupTests(unittest.TestCase):
    def setUp(self):
        temp_dir = "/tmp/opencode" if os.name != "nt" and Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(dir=temp_dir)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_command_source_and_frozen(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "platform", "linux"), patch.object(sys, "executable", "/usr/bin/python3"):
            with patch.object(sys, "frozen", False, create=True):
                config = self.root / "config"
                self.assertEqual(default_startup_command(config), (
                    "/usr/bin/python3", "-m", "nova_mic_switch.desktop", "--config-dir", str(config.absolute())
                ))
            with patch.object(sys, "frozen", True, create=True):
                self.assertEqual(default_startup_command(), ("/usr/bin/python3",))

    def test_frozen_linux_appimage_uses_original_file_and_extract_run(self):
        appimage = self.root / "Nova Mic Switch % release.AppImage"
        appimage.write_bytes(b"test AppImage")
        config = self.root / "profile with spaces"
        with patch.dict(os.environ, {"APPIMAGE": str(appimage)}, clear=False):
            with patch.object(sys, "platform", "linux"), patch.object(sys, "executable", "/tmp/.mount-temporary/NovaMicSwitch"):
                with patch.object(sys, "frozen", True, create=True):
                    command = default_startup_command(config)

        self.assertEqual(command, (
            str(appimage), "--appimage-extract-and-run", "--config-dir", str(config.absolute())
        ))
        manager = StartupManager(command, platform="linux", config_home=self.root / "xdg profile")
        manager.set_enabled(True, start_minimized=True)
        entry = (self.root / "xdg profile/autostart" / LINUX_FILENAME).read_text(encoding="utf-8")
        escaped_appimage = str(appimage).replace("%", "%%")
        self.assertIn(f'Exec="{escaped_appimage}" "--appimage-extract-and-run"', entry)
        self.assertEqual(entry.count("--tray"), 1)

    def test_frozen_linux_appimage_context_rejects_invalid_environment(self):
        invalid_values = (
            ("", "empty"),
            ("relative.AppImage", "absolute"),
            (str(self.root / "missing.AppImage"), "existing regular file"),
            (str(self.root / "bad\nname.AppImage"), "control characters"),
            (str(self.root / "bad\x7fname.AppImage"), "control characters"),
        )
        for value, message in invalid_values:
            with self.subTest(value=repr(value)), patch.dict(os.environ, {"APPIMAGE": value}, clear=False):
                with patch.object(sys, "platform", "linux"), patch.object(sys, "executable", "/tmp/.mount-temporary/NovaMicSwitch"):
                    with patch.object(sys, "frozen", True, create=True):
                        with self.assertRaisesRegex(StartupError, message):
                            default_startup_command(self.root / "profile")

    def test_source_and_windows_frozen_launch_ignore_appimage_environment(self):
        with patch.dict(os.environ, {"APPIMAGE": "invalid relative value\n"}, clear=False):
            with patch.object(sys, "platform", "linux"), patch.object(sys, "executable", "/usr/bin/python3"):
                with patch.object(sys, "frozen", False, create=True):
                    self.assertEqual(default_startup_command(), (
                        "/usr/bin/python3", "-m", "nova_mic_switch.desktop"
                    ))
            with patch.object(sys, "platform", "win32"), patch.object(sys, "executable", r"C:\Program Files\Nova\app.exe"):
                with patch.object(sys, "frozen", True, create=True):
                    self.assertEqual(default_startup_command(), (r"C:\Program Files\Nova\app.exe",))

    def test_default_windows_command_prefers_pythonw(self):
        python = self.root / "python.exe"
        pythonw = self.root / "pythonw.exe"
        pythonw.touch()
        with patch.object(sys, "platform", "win32"), patch.object(sys, "executable", str(python)):
            with patch.object(sys, "frozen", False, create=True):
                self.assertEqual(default_startup_command(), (
                    ntpath.abspath(str(pythonw)), "-m", "nova_mic_switch.desktop"
                ))

    def test_windows_readonly_installer_registration_and_own_value_updates(self):
        registry = FakeRegistry()
        manager = StartupManager((r"C:\Nova\app.exe", "--config-dir", r"C:\Users\me\cfg"),
                                 platform="win32", registry=registry)
        self.assertTrue(manager.available)
        self.assertFalse(manager.is_enabled())
        self.assertFalse(any(call[0] in ("set", "create") for call in registry.calls))
        registry.keys[WINDOWS_RUN_KEY] = {
            "Unrelated": ("keep", registry.REG_SZ),
            WINDOWS_VALUE_NAME: (subprocess.list2cmdline([r"C:\Nova\app.exe", "--tray"]), registry.REG_SZ),
        }
        registry.calls.clear()
        self.assertTrue(manager.is_enabled())
        self.assertFalse(any(call[0] in ("set", "create", "delete") for call in registry.calls))
        manager.set_enabled(True)
        expected = subprocess.list2cmdline([r"C:\Nova\app.exe", "--config-dir", r"C:\Users\me\cfg", "--tray"])
        self.assertEqual(registry.keys[WINDOWS_RUN_KEY], {
            "Unrelated": ("keep", registry.REG_SZ), WINDOWS_VALUE_NAME: (expected, registry.REG_SZ)
        })
        self.assertTrue(manager.is_enabled())
        manager.set_enabled(True, start_minimized=False)
        self.assertEqual(registry.keys[WINDOWS_RUN_KEY][WINDOWS_VALUE_NAME], (
            subprocess.list2cmdline([r"C:\Nova\app.exe", "--config-dir", r"C:\Users\me\cfg"]), registry.REG_SZ
        ))
        manager.set_enabled(False)
        manager.set_enabled(False)
        self.assertEqual(registry.keys[WINDOWS_RUN_KEY], {"Unrelated": ("keep", registry.REG_SZ)})

    def test_windows_missing_key_create_only_on_enable(self):
        registry = FakeRegistry()
        manager = StartupManager((r"C:\Nova\app.exe",), platform="win32", registry=registry)
        manager.set_enabled(False)
        self.assertNotIn(WINDOWS_RUN_KEY, registry.keys)
        self.assertFalse(any(call[0] == "create" for call in registry.calls))
        self.assertFalse(manager.is_enabled())
        self.assertNotIn(WINDOWS_RUN_KEY, registry.keys)
        manager.set_enabled(True)
        self.assertIn(WINDOWS_RUN_KEY, registry.keys)
        self.assertEqual([call[0] for call in registry.calls].count("create"), 1)

    def test_windows_validation_length_native_failure_and_readback(self):
        for invalid in ((), "app.exe", b"app.exe", ("relative.exe",), (r"C:\app.exe", "bad\narg")):
            with self.subTest(invalid=invalid), self.assertRaises(StartupError):
                StartupManager(invalid, platform="win32")
        registry = FakeRegistry()
        manager = StartupManager(("C:\\" + "x" * 260,), platform="win32", registry=registry)
        with self.assertRaisesRegex(StartupError, "260-character"):
            manager.set_enabled(True)
        self.assertFalse(any(call[0] in ("open", "create", "set") for call in registry.calls))
        registry = FakeRegistry()
        registry.fail_set = PermissionError("denied")
        with self.assertRaises(StartupError) as error:
            StartupManager((r"C:\app.exe",), platform="win32", registry=registry).set_enabled(True)
        self.assertIsInstance(error.exception.__cause__, PermissionError)
        registry = FakeRegistry()
        registry.wrong_readback = True
        with self.assertRaisesRegex(StartupError, "does not match"):
            StartupManager((r"C:\app.exe",), platform="win32", registry=registry).set_enabled(True)

    def test_windows_registration_presence_and_wrong_registry_type(self):
        registry = FakeRegistry()
        registry.keys[WINDOWS_RUN_KEY] = {WINDOWS_VALUE_NAME: ("installer legacy command", registry.REG_SZ)}
        manager = StartupManager((r"C:\app.exe", "--config-dir", r"C:\new"), platform="win32", registry=registry)
        self.assertTrue(manager.is_enabled())
        registry.keys[WINDOWS_RUN_KEY][WINDOWS_VALUE_NAME] = ("installer legacy command", 99)
        self.assertFalse(manager.is_enabled())

    def test_linux_registration_content_update_and_escaping(self):
        config = self.root / "cfg space" / 'quote"' / "back\\slash%$`"
        four_backslashes = "four" + "\\" * 4 + "slashes"
        command = ("/opt/nova app%/bin", "arg with space", 'q"uote', "back\\slash",
                   four_backslashes, "$HOME", "`touch`", "%f")
        manager = StartupManager(command, platform="linux", config_home=config)
        path = config / "autostart" / LINUX_FILENAME
        self.assertFalse(manager.is_enabled())
        self.assertFalse(path.parent.exists())
        manager.set_enabled(True)
        content = path.read_text(encoding="utf-8")
        self.assertIn('Exec="/opt/nova app%%/bin"', content)
        self.assertIn("%%f", content)
        self.assertIn('q\\\\"uote', content)
        self.assertIn('"back\\\\\\\\slash"', content)
        self.assertIn('"four' + "\\" * 16 + 'slashes"', content)
        self.assertIn('"\\\\$HOME"', content)
        self.assertIn('"\\\\`touch\\\\`"', content)
        self.assertTrue(manager.is_enabled())
        stale = StartupManager(("/opt/another-version",), platform="linux", config_home=config)
        self.assertTrue(stale.is_enabled())
        manager.set_enabled(True, start_minimized=False)
        self.assertNotIn("--tray", path.read_text(encoding="utf-8"))
        self.assertTrue(manager.is_enabled())
        manager.set_enabled(False)
        self.assertFalse(path.exists())
        manager.set_enabled(False)
        self.assertFalse(path.exists())

    def test_linux_rejects_collision_symlink_and_invalid_utf8(self):
        config = self.root / "config"
        manager = StartupManager(("/bin/app",), platform="linux", config_home=config)
        path = config / "autostart" / LINUX_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text("foreign data")
        with self.assertRaisesRegex(StartupError, "non-owned"):
            manager.set_enabled(True)
        self.assertEqual(path.read_text(), "foreign data")
        path.write_bytes(b"\xff")
        with self.assertRaises(StartupError):
            manager.is_enabled()
        with self.assertRaises(StartupError):
            manager.set_enabled(True)
        outside = self.root / "outside"
        outside.write_text("keep")
        if os.name == "nt":
            path.write_text("simulated symlink entry")
            real_is_symlink = Path.is_symlink

            def simulated_symlink(candidate):
                if candidate == path:
                    return True
                return real_is_symlink(candidate)

            with patch.object(Path, "is_symlink", autospec=True, side_effect=simulated_symlink):
                with self.assertRaisesRegex(StartupError, "symlink"):
                    manager.set_enabled(True)
                with self.assertRaisesRegex(StartupError, "symlink"):
                    manager.set_enabled(False)
            self.assertEqual(path.read_text(), "simulated symlink entry")
        else:
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaisesRegex(StartupError, "symlink"):
                manager.set_enabled(True)
            with self.assertRaisesRegex(StartupError, "symlink"):
                manager.set_enabled(False)
        self.assertEqual(outside.read_text(), "keep")

    def test_linux_atomic_failure_preserves_existing_owned_entry(self):
        config = self.root / "config"
        manager = StartupManager(("/bin/app",), platform="linux", config_home=config)
        path = config / "autostart" / LINUX_FILENAME
        manager.set_enabled(True)
        original = path.read_bytes()
        with patch("nova_mic_switch.startup.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(StartupError, "replace failed"):
                manager.set_enabled(True, start_minimized=False)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_linux_rejects_equals_before_filesystem_changes_and_validates_removal(self):
        config = self.root / "config"
        manager = StartupManager(("/bin/app=name",), platform="linux", config_home=config)
        with self.assertRaisesRegex(StartupError, "cannot contain '='"):
            manager.set_enabled(True)
        self.assertFalse(config.exists())
        manager = StartupManager(("/bin/app",), platform="linux", config_home=config)
        path = config / "autostart" / LINUX_FILENAME
        manager.set_enabled(True)
        with patch.object(Path, "unlink", autospec=True, side_effect=lambda self: None):
            with self.assertRaisesRegex(StartupError, "remains after removal"):
                manager.set_enabled(False)
        self.assertTrue(path.exists())

    def test_linux_xdg_home_and_unsupported_platform(self):
        manager = StartupManager(("/bin/app",), platform="linux",
                                 environ={"XDG_CONFIG_HOME": str(self.root / "xdg")})
        self.assertEqual(manager._linux_path, self.root / "xdg" / "autostart" / LINUX_FILENAME)
        unsupported = StartupManager(("app",), platform="darwin", config_home=self.root)
        self.assertFalse(unsupported.available)
        with self.assertRaisesRegex(StartupError, "not supported"):
            unsupported.is_enabled()
        with self.assertRaises(TypeError):
            manager.set_enabled(1)


if __name__ == "__main__":
    unittest.main()
