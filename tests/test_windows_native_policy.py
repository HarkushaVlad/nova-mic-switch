import tempfile
import unittest
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

POLICY_PATH = Path(__file__).parents[1] / "packaging" / "windows" / "native_policy.py"
POLICY_SPEC = importlib.util.spec_from_file_location("nova_windows_native_policy", POLICY_PATH)
POLICY = importlib.util.module_from_spec(POLICY_SPEC)
POLICY_SPEC.loader.exec_module(POLICY)
apply_windows_native_policy = POLICY.apply_windows_native_policy
canonical_windows_path = POLICY.canonical_windows_path


class WindowsNativePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def analysis(self, binaries):
        return SimpleNamespace(binaries=binaries, datas=[])

    def test_filters_optional_openssl_and_os_dlls_but_keeps_python_crypto(self):
        rows = [
            ("PySide6/Qt/plugins/tls/qopensslbackend.dll", r"C:\app\.venv\Lib\site-packages\PySide6\plugins\tls\qopensslbackend.dll", "BINARY"),
            ("PySide6/Qt/plugins/tls/qschannelbackend.dll", r"C:\app\.venv\Lib\site-packages\PySide6\plugins\tls\qschannelbackend.dll", "BINARY"),
            ("libcrypto-3-x64.dll", r"C:\Program Files\MySQL\bin\libcrypto-3-x64.dll", "BINARY"),
            ("libssl-3-x64.dll", r"C:\Program Files\MySQL\bin\libssl-3-x64.dll", "BINARY"),
            ("libcrypto-3.dll", r"C:\app\.venv\DLLs\libcrypto-3.dll", "BINARY"),
            ("ucrtbase.dll", r"C:\Python313\DLLs\ucrtbase.dll", "BINARY"),
            ("api-ms-win-core-file-l1-1-0.dll", r"C:\app\.venv\DLLs\api-ms-win-core-file-l1-1-0.dll", "BINARY"),
        ]
        analysis = self.analysis(rows)
        analysis.datas = [("qopensslbackend.dll", "unused", "DATA"), ("libssl-3-x64.dll", "unused", "DATA")]
        with patch.object(POLICY, "_write_evidence"), patch.object(POLICY.os.path, "isfile", return_value=True), patch.object(
            POLICY, "_pe_imports", return_value=set()
        ):
            apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")
        names = [row[0].split("/")[-1].casefold() for row in analysis.binaries]
        self.assertIn("qschannelbackend.dll", names)
        self.assertIn("libcrypto-3.dll", names)
        self.assertNotIn("qopensslbackend.dll", names)
        self.assertNotIn("libcrypto-3-x64.dll", names)
        self.assertNotIn("libssl-3-x64.dll", names)
        self.assertNotIn("ucrtbase.dll", names)
        self.assertFalse(analysis.datas)

    def test_removed_ssl_dependency_chain_is_not_counted_as_a_retained_import(self):
        rows = [
            ("qopensslbackend.dll", r"C:\app\.venv\qopensslbackend.dll", "BINARY"),
            ("qschannelbackend.dll", r"C:\app\.venv\qschannelbackend.dll", "BINARY"),
            ("libssl-3-x64.dll", r"C:\app\.venv\libssl-3-x64.dll", "BINARY"),
            ("libcrypto-3-x64.dll", r"C:\app\.venv\libcrypto-3-x64.dll", "BINARY"),
        ]
        analysis = self.analysis(rows)
        scanned = []

        def imports(filename):
            scanned.append(filename)
            if filename.endswith("libssl-3-x64.dll"):
                return {"libcrypto-3-x64.dll"}
            return set()

        with patch.object(POLICY, "_write_evidence"), patch.object(POLICY.os.path, "isfile", return_value=True), patch.object(
            POLICY, "_pe_imports", side_effect=imports
        ):
            apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")
        self.assertEqual(scanned, [r"C:\app\.venv\qschannelbackend.dll"])
        self.assertEqual([row[0] for row in analysis.binaries], ["qschannelbackend.dll"])

    def test_required_external_openssl_import_fails_clearly(self):
        analysis = self.analysis([
            ("extension.pyd", r"C:\app\.venv\Lib\extension.pyd", "EXTENSION"),
            ("libcrypto-3-x64.dll", r"C:\app\.venv\libcrypto-3-x64.dll", "BINARY"),
        ])
        with patch.object(POLICY.os.path, "isfile", return_value=True), patch.object(
            POLICY, "_pe_imports", return_value={"libcrypto-3-x64.dll"}
        ):
            with self.assertRaisesRegex(RuntimeError, "requires external OpenSSL"):
                apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")

    def test_tls_requires_schannel_when_openssl_plugin_exists(self):
        analysis = self.analysis([
            ("qopensslbackend.dll", r"C:\app\.venv\qopensslbackend.dll", "BINARY"),
        ])
        with self.assertRaisesRegex(RuntimeError, "qschannelbackend.dll is missing"):
            with patch.object(POLICY, "_write_evidence"):
                apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")

    def test_unapproved_global_native_origin_fails_and_case_paths_work(self):
        analysis = self.analysis([
            ("bad.dll", r"C:\Program Files\Temurin\bin\bad.dll", "BINARY"),
        ])
        with patch.object(POLICY.os.path, "isfile", return_value=True), patch.object(POLICY, "_pe_imports", return_value=set()):
            with self.assertRaisesRegex(RuntimeError, "destination=bad.dll.*Temurin"):
                apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")
        self.assertEqual(canonical_windows_path(r"C:\APP\.VENV\x.dll"), r"c:\app\.venv\x.dll")

    def test_os_root_is_not_implicitly_an_approved_payload_root(self):
        analysis = self.analysis([
            ("foreign.dll", r"C:\Windows\System32\foreign.dll", "BINARY"),
        ])
        with patch.object(POLICY.os.path, "isfile", return_value=True), patch.object(POLICY, "_pe_imports", return_value=set()):
            with self.assertRaisesRegex(RuntimeError, "Unapproved native payload origin"):
                apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")

    def test_existing_junction_escape_is_resolved_before_root_check(self):
        source = r"C:\app\.venv\junction\foreign.dll"
        analysis = self.analysis([("foreign.dll", source, "BINARY")])

        def realpath(path):
            return r"C:\outside\foreign.dll" if path == source else path

        with patch.object(POLICY.os, "name", "nt"), patch.object(POLICY.os.path, "realpath", side_effect=realpath), patch.object(
            POLICY.os.path, "isfile", return_value=True
        ), patch.object(POLICY, "_pe_imports", return_value=set()):
            with self.assertRaisesRegex(RuntimeError, "Unapproved native payload origin"):
                apply_windows_native_policy(analysis, r"C:\app", r"C:\app\.venv", r"C:\Python313")

    def test_unparseable_delay_imports_fail_closed_and_close_pe(self):
        class FakePEFormatError(Exception):
            pass

        class FakePE:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.DIRECTORY_ENTRY_IMPORT = []
                self.closed = False
                self.instances.append(self)

            def parse_data_directories(self, directories):
                raise FakePEFormatError("bad delay directory")

            def close(self):
                self.closed = True

        fake_pefile = SimpleNamespace(
            PE=FakePE,
            PEFormatError=FakePEFormatError,
            DIRECTORY_ENTRY={"IMAGE_DIRECTORY_ENTRY_IMPORT": 1, "IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT": 2},
        )
        with patch.dict(sys.modules, {"pefile": fake_pefile}):
            with self.assertRaisesRegex(RuntimeError, "Could not parse retained native PE imports"):
                POLICY._pe_imports("broken.dll")
        self.assertTrue(FakePE.instances[0].closed)

    def test_build_script_limits_only_pyinstaller_child_path_and_restores_it(self):
        script = (Path(__file__).parents[1] / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("$InheritedPath = $env:PATH", script)
        self.assertIn("$env:PATH = $AllowedPath -join", script)
        self.assertIn("& $Python -m PyInstaller", script)
        self.assertIn("$env:PATH = $InheritedPath", script)
        self.assertLess(script.index("$env:PATH = $AllowedPath"), script.index("& $Python -m PyInstaller"))
        self.assertLess(script.index("& $Python -m PyInstaller"), script.index("$env:PATH = $InheritedPath"))

    def test_evidence_selects_standard_microsoft_runtime_set_and_adds_version_metadata(self):
        self.assertEqual(POLICY.MICROSOFT_RUNTIME_DLLS, {
            "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
            "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll",
        })
        rows = []
        for name in sorted(POLICY.MICROSOFT_RUNTIME_DLLS):
            path = self.root / name
            path.write_bytes(b"synthetic PE fixture")
            rows.append((name, str(path), "BINARY"))

        class FakePE:
            FILE_HEADER = SimpleNamespace(Machine=0x8664)

            def __init__(self, *_args, **_kwargs):
                pass

            def close(self):
                pass

        fake_pefile = SimpleNamespace(PE=FakePE, PEFormatError=Exception)
        metadata = {
            "CompanyName": "Microsoft Corporation", "ProductName": "Microsoft® Visual Studio®",
            "FileVersion": "14.40.33810.0", "LegalCopyright": "Copyright © Microsoft Corporation.",
            "IsDebug": False, "IsPreRelease": False,
        }
        completed = SimpleNamespace(stdout=json.dumps(metadata, ensure_ascii=False))
        with patch.dict(sys.modules, {"pefile": fake_pefile}), \
             patch.object(POLICY.subprocess, "run", return_value=completed) as run, \
             patch.dict(POLICY.os.environ, {"SystemRoot": str(self.root)}):
            POLICY._write_evidence(rows, self.root)
            expected_powershell = str(self.root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            for invocation in run.call_args_list:
                command = invocation.args[0]
                self.assertEqual(command[0], expected_powershell)
                self.assertTrue(Path(command[0]).is_absolute())
                self.assertIn("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);", command[-1])
                self.assertTrue(command[-1].endswith(
                    "(Get-Item -LiteralPath $env:NOVA_DLL_EVIDENCE_PATH).VersionInfo | ConvertTo-Json -Compress"
                ))
                self.assertEqual(invocation.kwargs["encoding"], "utf-8")
                self.assertEqual(invocation.kwargs["errors"], "strict")
                self.assertEqual(invocation.kwargs["timeout"], 10)
                self.assertTrue(invocation.kwargs["capture_output"])
                self.assertTrue(invocation.kwargs["text"])
                self.assertTrue(invocation.kwargs["check"])
                self.assertEqual(
                    invocation.kwargs["env"]["NOVA_DLL_EVIDENCE_PATH"],
                    str(rows[run.call_args_list.index(invocation)][1]),
                )
        evidence_path = self.root / "build" / "native-evidence-windows.json"
        evidence_bytes = evidence_path.read_bytes()
        self.assertIn("Microsoft® Visual Studio®".encode("utf-8"), evidence_bytes)
        self.assertIn("Copyright © Microsoft Corporation.".encode("utf-8"), evidence_bytes)
        records = json.loads(evidence_bytes.decode("utf-8"))
        self.assertEqual({Path(row["destination"]).name.casefold() for row in records}, POLICY.MICROSOFT_RUNTIME_DLLS)
        for item in records:
            self.assertEqual(item["product"], "Microsoft® Visual Studio®")
            self.assertEqual(item["legal_copyright"], "Copyright © Microsoft Corporation.")
            self.assertIs(item["is_debug"], False)
            self.assertIs(item["is_pre_release"], False)
        previous_evidence = evidence_path.read_bytes()
        with patch.dict(sys.modules, {"pefile": fake_pefile}), \
             patch.object(POLICY.subprocess, "run", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")), \
             patch.dict(POLICY.os.environ, {"SystemRoot": str(self.root)}):
            with self.assertRaisesRegex(RuntimeError, "Could not read native DLL version metadata"):
                POLICY._write_evidence(rows, self.root)
        self.assertEqual(evidence_path.read_bytes(), previous_evidence)


if __name__ == "__main__":
    unittest.main()
