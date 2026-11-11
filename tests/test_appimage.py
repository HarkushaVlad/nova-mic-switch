"""Offline structural tests for AppImage staging and tool invocation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_appimage.sh"


@unittest.skipUnless(sys.platform.startswith("linux"), "AppImage packaging requires Linux")
class AppImageBuildScriptTests(unittest.TestCase):
    def setUp(self):
        fixture_root = "/tmp/opencode" if sys.platform.startswith("linux") and Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="nova appimage test ", dir=fixture_root)
        self.root = Path(self.temp.name) / "project root with spaces"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "packaging/linux").mkdir(parents=True)
        (self.root / "packaging/icons").mkdir(parents=True)
        (self.root / ".venv/bin").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "scripts/build_appimage.sh")
        shutil.copy2(ROOT / "packaging/linux/AppRun", self.root / "packaging/linux/AppRun")
        shutil.copy2(ROOT / "packaging/linux/nova-mic-switch.desktop", self.root / "packaging/linux/nova-mic-switch.desktop")
        (self.root / "packaging/icons/nova-mic-switch.svg").write_text("<svg/>\n", encoding="utf-8")
        (self.root / "packaging/icons/nova-mic-switch.png").write_bytes(b"fake PNG icon")
        (self.root / "pyproject.toml").write_text('[project]\nname="example"\nversion="9.8.7"\n', encoding="utf-8")
        python = Path(os.path.realpath(os.sys.executable))
        (self.root / ".venv/bin/python").symlink_to(python)

        self.bundle = self.root / "input bundle"
        (self.bundle / "_internal").mkdir(parents=True)
        (self.bundle / "_internal").chmod(0o700)
        executable = self.bundle / "NovaMicSwitch"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
        payload = self.bundle / "_internal/example"
        payload.write_text("bundle payload", encoding="utf-8")
        payload.chmod(0o600)

        self.runtime = self.root / "verified runtime"
        self.runtime.write_bytes(b"runtime placeholder")
        self.tool_args = self.root / "tool args"
        self.appdir_contents = self.root / "AppDir contents"
        self.desktop_copy = self.root / "desktop captured by tool"
        self.tool_environment = self.root / "tool environment"
        self.tool = self.root / "trusted tools/appimagetool"
        self.tool.parent.mkdir(parents=True)
        self.tool.write_text(
            "#!/bin/sh\n"
            "printf '%s\\0' \"$@\" > \"$TOOL_ARGS\"\n"
            "appdir=$3\noutput=$4\n"
            "find \"$appdir\" -mindepth 1 -printf '%P\\n' | sort > \"$APPDIR_CONTENTS\"\n"
            "test -x \"$appdir/AppRun\" && test -f \"$appdir/usr/lib/nova-mic-switch/NovaMicSwitch\" && test -L \"$appdir/.DirIcon\" || exit 30\n"
            "test \"$(readlink \"$appdir/.DirIcon\")\" = nova-mic-switch.png || exit 35\n"
            "test \"$(stat -c %a \"$appdir/usr/lib/nova-mic-switch\")\" = 755 || exit 31\n"
            "test \"$(stat -c %a \"$appdir/usr/lib/nova-mic-switch/_internal\")\" = 755 || exit 32\n"
            "test \"$(stat -c %a \"$appdir/usr/lib/nova-mic-switch/NovaMicSwitch\")\" = 755 || exit 33\n"
            "test \"$(stat -c %a \"$appdir/usr/lib/nova-mic-switch/_internal/example\")\" = 644 || exit 34\n"
            "cp \"$appdir/nova-mic-switch.desktop\" \"$DESKTOP_CAPTURE\"\n"
            "printf '%s %s\\n' \"$ARCH\" \"$APPIMAGE_EXTRACT_AND_RUN\" > \"$TOOL_ENV\"\n"
            "printf 'fake AppImage payload' > \"$output\"\n",
            encoding="utf-8",
        )
        self.tool.chmod(0o755)
        self.output = self.root / "output with spaces"
        self.other_output = self.output / "unrelated-file.txt"
        self.other_output.parent.mkdir(parents=True)
        self.other_output.write_text("keep me", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args, env=None):
        process_env = os.environ.copy()
        process_env.pop("APPIMAGETOOL", None)
        process_env.pop("APPIMAGE_RUNTIME_FILE", None)
        process_env["TOOL_ARGS"] = str(self.tool_args)
        process_env["APPDIR_CONTENTS"] = str(self.appdir_contents)
        process_env["DESKTOP_CAPTURE"] = str(self.desktop_copy)
        process_env["TOOL_ENV"] = str(self.tool_environment)
        if env:
            process_env.update(env)
        return subprocess.run(
            ["bash", str(self.root / "scripts/build_appimage.sh"), *map(str, args)],
            cwd=self.root,
            env=process_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def build(self, *extra):
        return self.run_script(
            "--appimagetool", self.tool,
            "--runtime-file", self.runtime,
            "--bundle", self.bundle,
            "--output-dir", self.output,
            *extra,
        )

    def test_help_does_not_require_tools_or_artifacts(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runtime-file", result.stdout)
        self.assertFalse(self.tool_args.exists())

    def test_build_stages_complete_appdir_and_emits_versioned_checksum(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)

        output = self.output / "NovaMicSwitch-9.8.7-x86_64.AppImage"
        checksum = self.output / "NovaMicSwitch-9.8.7-x86_64.AppImage.sha256"
        self.assertEqual(output.read_bytes(), b"fake AppImage payload")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        self.assertEqual(checksum.read_text(encoding="utf-8"), f"{digest}  {output.name}\n")
        self.assertEqual(self.other_output.read_text(encoding="utf-8"), "keep me")

        args = self.tool_args.read_bytes().split(b"\0")[:-1]
        self.assertEqual(args[0:2], [b"--runtime-file", os.fsencode(self.runtime)])
        appdir = Path(os.fsdecode(args[2]))
        tool_output = Path(os.fsdecode(args[3]))
        self.assertEqual(tool_output.name, output.name)
        self.assertEqual(tool_output.parent.parent, self.output)
        self.assertTrue(appdir.name == "NovaMicSwitch.AppDir")
        self.assertEqual(appdir.parent.parent, self.root / "build/appimage")
        self.assertEqual(self.tool_environment.read_text(encoding="utf-8"), "x86_64 1\n")
        contents = self.appdir_contents.read_text(encoding="utf-8").splitlines()
        self.assertIn("AppRun", contents)
        self.assertIn("nova-mic-switch.desktop", contents)
        self.assertIn("nova-mic-switch.svg", contents)
        self.assertIn("nova-mic-switch.png", contents)
        self.assertIn("usr/lib/nova-mic-switch/NovaMicSwitch", contents)
        self.assertIn("usr/lib/nova-mic-switch/_internal/example", contents)
        self.assertIn(".DirIcon", contents)
        desktop = self.desktop_copy.read_text(encoding="utf-8")
        self.assertIn("Exec=AppRun", desktop)
        self.assertIn("Icon=nova-mic-switch", desktop)
        self.assertFalse(list((self.root / "build/appimage").glob("stage.*")))

    def test_build_accepts_separate_work_root_without_removing_siblings(self):
        work_root = self.root / "alternate work root"
        work_root.mkdir()
        sibling = work_root / "keep.txt"
        sibling.write_text("keep me", encoding="utf-8")
        result = self.build("--work-dir", work_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.tool_args.read_bytes().split(b"\0")[:-1]
        appdir = Path(os.fsdecode(args[2]))
        self.assertEqual(appdir.parent.parent, work_root)
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(list(work_root.glob("stage.*")))

    def test_app_run_resolves_spaces_and_passes_arguments(self):
        appdir = self.root / "test AppDir"
        executable = appdir / "usr/lib/nova-mic-switch/NovaMicSwitch"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$APPRUN_CAPTURE\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        apprun = appdir / "AppRun"
        shutil.copy2(ROOT / "packaging/linux/AppRun", apprun)
        apprun.chmod(0o755)
        capture = self.root / "app args"
        result = subprocess.run(
            [str(apprun), "--safe-mode", "--config-dir", "a path with spaces"],
            cwd=self.root,
            env={**os.environ, "APPRUN_CAPTURE": str(capture)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(capture.read_text(encoding="utf-8").splitlines(), ["--safe-mode", "--config-dir", "a path with spaces"])

    def test_refuses_missing_verified_runtime_before_tool_execution(self):
        result = self.run_script("--appimagetool", self.tool, "--bundle", self.bundle, "--output-dir", self.output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verified runtime", result.stderr)
        self.assertFalse(self.tool_args.exists())

    def test_refuses_relative_tool_path_before_execution(self):
        result = self.run_script("--appimagetool", "relative-tool", "--runtime-file", self.runtime)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be absolute", result.stderr)
        self.assertFalse(self.tool_args.exists())

    def test_refuses_missing_bundle_icon_and_runtime_without_touching_output(self):
        missing_bundle = self.root / "missing bundle"
        result = self.run_script(
            "--appimagetool", self.tool,
            "--runtime-file", self.runtime,
            "--bundle", missing_bundle,
            "--output-dir", self.output,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("onedir bundle does not exist", result.stderr)
        self.assertFalse(self.tool_args.exists())

        result = self.build("--icon", self.root / "missing.svg")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SVG icon is missing", result.stderr)
        self.assertFalse(self.tool_args.exists())
        self.assertEqual(self.other_output.read_text(encoding="utf-8"), "keep me")

    def test_refuses_unsafe_version_before_tool_execution(self):
        (self.root / "pyproject.toml").write_text('[project]\nname="example"\nversion="../unsafe"\n', encoding="utf-8")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe project version", result.stderr)
        self.assertFalse(self.tool_args.exists())
        self.assertEqual(self.other_output.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
