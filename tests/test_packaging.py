"""Offline structural checks for the platform packaging definitions."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_DIR = Path("/tmp/opencode") if os.name != "nt" and Path("/tmp/opencode").is_dir() else None


class PackagingDefinitionTests(unittest.TestCase):
    def test_frozen_entry_is_syntax_valid_and_delegates_to_desktop(self):
        source = (ROOT / "packaging/frozen_entry.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("from nova_mic_switch.desktop import main", source)
        self.assertIn("raise SystemExit(main())", source)

    def test_spec_has_explicit_package_path_and_platform_hid_backend(self):
        source = (ROOT / "packaging/nova-mic-switch.spec").read_text(encoding="utf-8")
        ast.parse(source)

        class FakeAnalysis:
            def __init__(self, scripts, **kwargs):
                self.scripts = scripts
                self.pure = []
                self.binaries = [
                    ("Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so", "/qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so", "BINARY"),
                    ("Qt/bin/Qt6Pdf.dll", "/qt/bin/Qt6Pdf.dll", "BINARY"),
                    ("Qt/lib/libQt6PdfWidgets.so.6", "/qt/lib/libQt6PdfWidgets.so.6", "BINARY"),
                    ("Qt/plugins/imageformats/libqpdf.so", "/qt/plugins/imageformats/libqpdf.so", "BINARY"),
                    ("Qt/lib/libQt6Multimedia.so.6", "/qt/lib/libQt6Multimedia.so.6", "BINARY"),
                    ("Qt/plugins/imageformats/libqsvg.so", "/qt/plugins/imageformats/libqsvg.so", "BINARY"),
                    ("Qt/lib/libQt6Quick.so.6", "/qt/lib/libQt6Quick.so.6", "BINARY"),
                    ("Qt/plugins/platforms/libqxcb.so", "/qt/plugins/platforms/libqxcb.so", "BINARY"),
                ]
                self.datas = kwargs["datas"] + [
                    ("Qt/plugins/platforminputcontexts/qtvirtualkeyboard.json", "/qt/plugins/platforminputcontexts/qtvirtualkeyboard.json", "DATA"),
                    ("Qt/lib/Qt6Pdf.dll", "/qt/lib/Qt6Pdf.dll", "DATA"),
                    ("Qt/translations/qtbase_en.qm", "/qt/translations/qtbase_en.qm", "DATA"),
                ]
                captures.append((scripts, kwargs))

        captures = []
        license_tmp = tempfile.TemporaryDirectory(prefix="nova-spec-licenses-", dir=TEST_TEMP_DIR)
        self.addCleanup(license_tmp.cleanup)
        licenses_dir = Path(license_tmp.name)
        (licenses_dir / "THIRD-PARTY-NOTICES.md").write_text("fixture notices", encoding="utf-8")
        (licenses_dir / "sample-license.txt").write_text("fixture license", encoding="utf-8")
        (licenses_dir / "qt-6.12.0").mkdir()
        (licenses_dir / "qt-6.12.0/qt-license.txt").write_text("qt fixture", encoding="utf-8")
        (licenses_dir / "ffmpeg-9.0.1").mkdir()
        (licenses_dir / "ffmpeg-9.0.1/ffmpeg-license.txt").write_text("ffmpeg fixture", encoding="utf-8")
        (licenses_dir / "manifest.json").write_text(
            json.dumps({"texts": [
                {"destination": "sample-license.txt"},
                {"destination": "qt-6.12.0/qt-license.txt"},
                {"destination": "ffmpeg-9.0.1/ffmpeg-license.txt"},
            ]}), encoding="utf-8"
        )
        license_module = ModuleType("nova_license_collector")
        license_module.ensure_licenses = lambda output: licenses_dir
        pyinstaller = ModuleType("PyInstaller")
        hooks = ModuleType("PyInstaller.utils.hooks")
        hooks.collect_data_files = lambda package, **kwargs: [(package, "data")]
        metadata_calls = []

        def fake_copy_metadata(distribution):
            metadata_calls.append(distribution)
            return [(f"metadata:{distribution}", distribution)]

        hooks.copy_metadata = fake_copy_metadata
        pyinstaller_utils = ModuleType("PyInstaller.utils")
        pyinstaller_utils.hooks = hooks
        pyinstaller.utils = pyinstaller_utils
        namespace = {
            "SPECPATH": str(ROOT / "packaging"),
            "Analysis": FakeAnalysis,
            "PYZ": lambda value: value,
            "EXE": lambda *args, **kwargs: SimpleNamespace(args=args, kwargs=kwargs),
            "COLLECT": lambda *args, **kwargs: SimpleNamespace(args=args, kwargs=kwargs),
        }

        for platform, expected_backend, other_backend in (
            ("linux", "hidraw", "hid"),
            ("win32", "hid", "hidraw"),
        ):
            captures.clear()
            metadata_calls.clear()
            namespace.update({"__file__": str(ROOT / "packaging/nova-mic-switch.spec")})
            with patch.object(sys, "platform", platform), patch.dict(
                sys.modules,
                {
                    "nova_license_collector": license_module,
                    "PyInstaller": pyinstaller,
                    "PyInstaller.utils": pyinstaller_utils,
                    "PyInstaller.utils.hooks": hooks,
                },
            ):
                exec(compile(source, namespace["__file__"], "exec"), namespace)

            scripts, kwargs = captures[0]
            self.assertEqual(scripts, [str(ROOT / "packaging/frozen_entry.py")])
            self.assertTrue(Path(scripts[0]).is_file())
            self.assertEqual(kwargs["pathex"], [str(ROOT / "src")])
            self.assertIn(expected_backend, kwargs["hiddenimports"])
            self.assertNotIn(other_backend, kwargs["hiddenimports"])
            self.assertEqual(kwargs["binaries"], [])
            datas = namespace["a"].datas
            binary_names = [row[0].lower() for row in namespace["a"].binaries]
            data_names = [row[0].lower() for row in datas if isinstance(row, tuple) and len(row) == 3]
            retained = binary_names + data_names
            self.assertFalse(any("virtualkeyboard" in name or "pdf" in name for name in retained))
            self.assertTrue(any("multimedia" in name for name in binary_names))
            self.assertTrue(any("qsvg" in name for name in binary_names))
            self.assertTrue(any("qt6quick" in name for name in binary_names))
            self.assertTrue(any("platforms/libqxcb" in name for name in binary_names))
            license_datas = [item for item in datas if isinstance(item, tuple) and len(item) == 2 and str(item[1]).startswith("licenses")]
            self.assertTrue(any(Path(item[0]).name == "THIRD-PARTY-NOTICES.md" and item[1] == "licenses" for item in license_datas))
            self.assertTrue(any(Path(item[0]).name == "manifest.json" and item[1] == "licenses" for item in license_datas))
            self.assertTrue(any(item[1] == "licenses/qt-6.12.0" for item in license_datas))
            self.assertTrue(any(item[1] == "licenses/ffmpeg-9.0.1" for item in license_datas))
            self.assertEqual(
                metadata_calls,
                ["PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "hidapi", "pyinstaller"],
            )
            self.assertIn('"PySide6.QtWebEngineCore"', source)
            for excluded in ("PySide6.QtVirtualKeyboard", "PySide6.QtPdf", "PySide6.QtPdfWidgets"):
                self.assertIn(excluded, namespace["excludes"])
            self.assertNotIn("collect_all", source)
            self.assertTrue(namespace["exe"].kwargs["exclude_binaries"])
            self.assertFalse(namespace["exe"].kwargs["console"])
            if platform == "win32":
                self.assertEqual(namespace["exe"].kwargs["icon"], str(ROOT / "packaging/icons/nova-mic-switch.ico"))
            else:
                self.assertNotIn("icon", namespace["exe"].kwargs)

    def test_copied_metadata_and_installed_notice_files(self):
        distributions = {
            name: importlib.metadata.distribution(name)
            for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "hidapi", "pyinstaller")
        }
        for name, distribution in distributions.items():
            self.assertTrue(distribution.files, f"{name} must have installed distribution metadata")
        for name in ("hidapi", "pyinstaller"):
            distribution = distributions[name]
            notice_paths = [
                distribution.locate_file(path)
                for path in distribution.files
                if any(term in str(path).lower() for term in ("license", "copying", "notice"))
            ]
            self.assertTrue(notice_paths, f"{name} should supply a notice/license file")
            self.assertTrue(all(path.is_file() for path in notice_paths))

    def test_installer_is_per_user_and_owns_only_its_run_value(self):
        source = (ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", source)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\Nova Mic Switch", source)
        startup_task = next(line for line in source.splitlines() if line.startswith('Name: "startup";'))
        self.assertIn('GroupDescription: "Startup:"', startup_task)
        self.assertNotIn("unchecked", startup_task)
        self.assertIn('Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"', source)
        self.assertIn('ValueName: "NovaMicSwitch"', source)
        self.assertIn('Tasks: startup; Flags: uninsdeletevalue', source)
        self.assertIn('ValueType: none; ValueName: "NovaMicSwitch"; Tasks: not startup; Flags: deletevalue', source)
        self.assertNotIn("deletekey", source.lower())
        self.assertIn('Parameters: "--tray"', source)

    def test_build_scripts_target_only_native_platform_and_bundle_paths(self):
        linux = (ROOT / "scripts/build_linux.sh").read_text(encoding="utf-8")
        windows = (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn('uname -s', linux)
        self.assertIn('dist/linux', linux)
        self.assertIn('UV_CACHE_DIR', linux)
        self.assertIn('PYINSTALLER_CONFIG_DIR', linux)
        self.assertIn('Windows_NT', windows)
        self.assertIn('dist\\windows', windows)
        self.assertIn('InnoSetupCompiler', windows)
        self.assertIn('PYINSTALLER_CONFIG_DIR', windows)
        self.assertIn('installer.iss', windows)
        self.assertNotRegex(linux, re.compile(r"\brm\s+-rf\b"))
        self.assertNotRegex(windows, re.compile(r"\bRemove-Item\b", re.IGNORECASE))

    def test_installer_version_matches_project_version(self):
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        installer = (ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")
        project_version = re.search(r'^version\s*=\s*"([^"]+)"', project, re.MULTILINE)
        installer_version = re.search(r'^#define AppVersion "([^"]+)"', installer, re.MULTILINE)
        self.assertIsNotNone(project_version)
        self.assertIsNotNone(installer_version)
        self.assertEqual(installer_version.group(1), project_version.group(1))


if __name__ == "__main__":
    unittest.main()
