import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import native_report


class NativeReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.workpath = self.root / "work"
        self.output = self.root / "evidence"
        self.bundle.mkdir()
        self.workpath.mkdir()

    def write_toc(self, name, *rows):
        (self.workpath / name).write_text(repr(("toc", rows)), encoding="utf-8")

    def test_recursive_toc_wheel_vendor_mapping_hash_difference_and_unresolved_rows(self):
        shipped = self.bundle / "_internal" / "hidapi.libs" / "libhidapi.so.0"
        shipped.parent.mkdir(parents=True)
        shipped.write_bytes(b"patchelf-adjusted")
        source = self.root / "site-packages" / "hidapi.libs" / "libhidapi.so.0"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"original-wheel-file")
        extra_source = self.root / "alternate" / "libhidapi.so.0"
        extra_source.parent.mkdir()
        extra_source.write_bytes(b"second-candidate")
        unknown = self.bundle / "_internal" / "libunknown.so"
        unknown.write_bytes(b"unknown")
        executable = self.bundle / "NovaMicSwitch"
        executable.write_bytes(b"project-exe")
        self.write_toc("Analysis-00.toc",
            (("hidapi.libs/libhidapi.so.0", str(source), "BINARY"),),
            (("hidapi.libs/libhidapi.so.0", str(extra_source), "BINARY"),),
            (("NovaMicSwitch", str(self.root / "NovaMicSwitch"), "EXECUTABLE"),),
            (("hidapi.libs/libhidapi.so.0", str(extra_source), "DATA"),),
        )
        collect_source = self.root / "work" / "patched-cache" / "libhidapi.so.0"
        collect_source.parent.mkdir(parents=True)
        collect_source.write_bytes(b"collect cache content")
        self.write_toc("COLLECT-00.toc", (("hidapi.libs/libhidapi.so.0", str(collect_source), "BINARY"),))
        owner_map = {native_report._path_key(source): ("hidapi", "0.14.0")}
        with patch.object(native_report, "_wheel_owners", return_value=owner_map):
            report = native_report.build_report(self.bundle, self.workpath, self.output)

        by_path = {row["relative_bundle_path"]: row for row in report["native_files"]}
        resolved = by_path["_internal/hidapi.libs/libhidapi.so.0"]
        self.assertEqual(resolved["source_mapping"], "ambiguous")
        self.assertIsNone(resolved["original_input_path"])
        self.assertEqual(len(resolved["source_candidates"]), 2)
        self.assertNotIn(str(collect_source), resolved["source_candidates"])
        self.assertEqual(resolved["provider"]["kind"], "unresolved")
        unresolved = by_path["_internal/libunknown.so"]
        self.assertEqual(unresolved["provider"]["kind"], "unresolved")
        self.assertIsNone(unresolved["source_sha256"])
        project = by_path["NovaMicSwitch"]
        self.assertEqual(project["provider"]["kind"], "project-executable")
        self.assertIn("_internal/hidapi.libs/libhidapi.so.0", report["unresolved"])
        self.assertTrue((self.output / "report.json").is_file())
        self.assertEqual(json.loads((self.output / "report.json").read_text())["schema_version"], 1)

    def test_host_package_lookup_does_not_rewrite_original_canonical_source(self):
        shipped = self.bundle / "_internal" / "libQt6Core.so.6"
        shipped.parent.mkdir(parents=True)
        shipped.write_bytes(b"host library")
        canonical_source = Path("/usr/lib/x86_64-linux-gnu/libQt6Core.so.6")
        self.write_toc("Analysis-00.toc", (("libQt6Core.so.6", str(canonical_source), "BINARY"),))
        host = {
            "provider": {"name": "libqt6core6:amd64", "version": "6.8.2", "kind": "system-package"},
            "source_package": "qt6-base", "source_version": "6.8.2", "noticefiles": [],
            "notice_status": "copied",
        }
        with patch.object(native_report.sys, "platform", "linux"), \
             patch.object(native_report, "_wheel_owners", return_value={}), \
             patch.object(native_report, "_host_provider", return_value=host):
            report = native_report.build_report(self.bundle, self.workpath, self.output)
        row = report["native_files"][0]
        self.assertEqual(row["original_input_path"], str(canonical_source.resolve()))
        self.assertEqual(row["provider"]["kind"], "system-package")

    def test_unique_wheel_mapping_reports_original_hash_difference(self):
        shipped = self.bundle / "_internal" / "hidapi.libs" / "libhidapi.so.0"
        shipped.parent.mkdir(parents=True)
        shipped.write_bytes(b"patched")
        source = self.root / "site-packages" / "hidapi.libs" / "libhidapi.so.0"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"wheel original")
        collect_source = self.root / "work" / "patched-cache" / "libhidapi.so.0"
        collect_source.parent.mkdir(parents=True)
        collect_source.write_bytes(b"collect cache content")
        self.write_toc("Analysis-00.toc", (("hidapi.libs/libhidapi.so.0", str(source), "BINARY"),))
        self.write_toc("COLLECT-00.toc", (("hidapi.libs/libhidapi.so.0", str(collect_source), "BINARY"),))

        class FakeDistribution:
            metadata = {"Name": "hidapi"}
            version = "0.14.0"
            files = [Path("hidapi.libs/libhidapi.so.0")]

            def __init__(self, original_path):
                self.original_path = original_path

            def locate_file(self, _item):
                return self.original_path

        with patch.object(native_report.importlib.metadata, "distributions", return_value=[FakeDistribution(source)]):
            report = native_report.build_report(self.bundle, self.workpath, self.output)
        row = report["native_files"][0]
        self.assertEqual(row["source_mapping"], "resolved")
        self.assertEqual(row["provider"], {"name": "hidapi", "version": "0.14.0", "kind": "python-wheel"})
        self.assertEqual(row["source_package"], "hidapi")
        self.assertEqual(row["source_version"], "0.14.0")
        self.assertEqual(row["source_sha256"], hashlib.sha256(b"wheel original").hexdigest())
        self.assertTrue(row["source_hash_differs"])

    def test_exact_destinations_do_not_merge_distinct_same_basename_sources(self):
        source_a = self.root / "a" / "libcrypto.dll"
        source_b = self.root / "b" / "libcrypto.dll"
        source_a.parent.mkdir()
        source_b.parent.mkdir()
        source_a.write_bytes(b"A")
        source_b.write_bytes(b"B")
        for destination, payload in (("_internal/A/libcrypto.dll", b"shipped A"), ("_internal/B/libcrypto.dll", b"shipped B")):
            path = self.bundle / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.write_toc("Analysis-00.toc",
                       (("A/libcrypto.dll", str(source_a), "BINARY"),),
                       (("B/libcrypto.dll", str(source_b), "BINARY"),))
        with patch.object(native_report, "_wheel_owners", return_value={}):
            report = native_report.build_report(self.bundle, self.workpath, self.output)
        by_path = {row["relative_bundle_path"]: row for row in report["native_files"]}
        self.assertEqual(by_path["_internal/A/libcrypto.dll"]["original_input_path"], str(source_a.resolve()))
        self.assertEqual(by_path["_internal/B/libcrypto.dll"]["original_input_path"], str(source_b.resolve()))
        self.assertEqual(by_path["_internal/A/libcrypto.dll"]["source_mapping"], "resolved")
        self.assertEqual(by_path["_internal/B/libcrypto.dll"]["source_mapping"], "resolved")

    def test_unowned_venv_site_package_is_not_misclassified_as_python_runtime(self):
        prefix = self.root / "venv"
        base_prefix = self.root / "python"
        source = prefix / "Lib" / "site-packages" / "vendor.dll"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"unowned vendor binary")
        runtime_source = base_prefix / "lib" / "python3.13" / "lib-dynload" / "_runtime.so"
        with patch.object(native_report.sys, "prefix", str(prefix)), \
             patch.object(native_report.sys, "base_prefix", str(base_prefix)):
            provider = native_report._provider_for(source, "vendor.dll", {}, self.output, {})
            self.assertFalse(native_report._is_python_runtime_source(source))
            self.assertTrue(native_report._is_python_runtime_source(runtime_source))
        self.assertEqual(provider["provider"]["kind"], "unresolved")

    def test_windows_python_runtime_classification_only_accepts_runtime_binaries(self):
        prefix = self.root / "cpython"
        stdlib_extension = prefix / "DLLs" / "_sqlite3.pyd"
        library_extension = prefix / "Lib" / "encodings" / "_native.pyd"
        vendor_dll = prefix / "DLLs" / "mysql.dll"
        library_dll = prefix / "Lib" / "jvm.dll"
        python_dll = prefix / "python313.dll"
        for path in (stdlib_extension, library_extension, vendor_dll, library_dll, python_dll):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        site_extension = prefix / "Lib" / "site-packages" / "vendor.pyd"
        site_extension.parent.mkdir(parents=True)
        site_extension.write_bytes(b"vendor")
        with patch.object(native_report, "os", SimpleNamespace(name="nt")), \
             patch.object(native_report.sys, "prefix", str(prefix)), \
             patch.object(native_report.sys, "base_prefix", str(prefix)):
            self.assertTrue(native_report._is_python_runtime_source(stdlib_extension))
            self.assertTrue(native_report._is_python_runtime_source(library_extension))
            self.assertTrue(native_report._is_python_runtime_source(python_dll))
            self.assertFalse(native_report._is_python_runtime_source(vendor_dll))
            self.assertFalse(native_report._is_python_runtime_source(library_dll))
            self.assertFalse(native_report._is_python_runtime_source(site_extension))

    @unittest.skipUnless(sys.platform.startswith("linux"), "usrmerge paths are Linux-specific")
    def test_usrmerge_aliases_are_queried_only_for_same_existing_file(self):
        names = (
            "libQt6Core.so.6", "libQt6DBus.so.6", "libQt6Gui.so.6", "libQt6Multimedia.so.6",
            "libavcodec.so.60", "libavformat.so.60", "libavutil.so.58", "libswresample.so.4",
            "libswscale.so.7", "libhidapi-hidraw.so.0", "libusb-1.0.so.0", "libudev.so.1",
            "libstdc++.so.6",
        )
        for name in names:
            with self.subTest(name=name):
                source = Path(f"/usr/lib/x86_64-linux-gnu/{name}")
                alias = Path(f"/lib/x86_64-linux-gnu/{name}")
                queried = []

                def run_dpkg(arguments):
                    queried.append(arguments[-1])
                    return ("libsample1:amd64: " + str(alias) + "\n", "") if arguments[-1] == str(alias) else ("", "")

                with patch.object(Path, "resolve", autospec=True, side_effect=lambda self, **_kwargs: self), \
                     patch.object(Path, "is_file", autospec=True, side_effect=lambda self: self in {source, alias}), \
                     patch.object(native_report.os.path, "samefile", side_effect=lambda left, right: {Path(left), Path(right)} == {source, alias}), \
                     patch.object(native_report, "_run_dpkg", side_effect=run_dpkg):
                    self.assertEqual(native_report._package_for_path(source), "libsample1:amd64")
                self.assertEqual(queried, [str(source), str(alias)])

        source = Path("/usr/lib/x86_64-linux-gnu/libunrelated.so")
        alias = Path("/lib/x86_64-linux-gnu/libunrelated.so")
        queried = []
        with patch.object(Path, "resolve", autospec=True, side_effect=lambda self, **_kwargs: self), \
             patch.object(Path, "is_file", autospec=True, side_effect=lambda _self: True), \
             patch.object(native_report.os.path, "samefile", return_value=False), \
             patch.object(native_report, "_run_dpkg", side_effect=lambda args: (queried.append(args[-1]) or ("", ""))):
            self.assertIsNone(native_report._package_for_path(source))
        self.assertEqual(queried, [str(source)])

    def test_cpython_supplied_vc_runtime_has_explicit_provenance_not_fake_notice(self):
        prefix = self.root / "cpython"
        runtime = prefix / "DLLs" / "vcruntime140.dll"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"runtime")
        vendor = prefix / "DLLs" / "mysql.dll"
        vendor.write_bytes(b"not a CPython runtime")
        jvm = prefix / "DLLs" / "jvm.dll"
        jvm.write_bytes(b"not a CPython runtime")
        with patch.object(native_report.sys, "prefix", str(prefix)), \
             patch.object(native_report.sys, "base_prefix", str(prefix)):
            provider = native_report._provider_for(runtime, "vcruntime140.dll", {}, self.output, {})
            unknown = native_report._provider_for(vendor, "mysql.dll", {}, self.output, {})
            unknown_jvm = native_report._provider_for(jvm, "jvm.dll", {}, self.output, {})
        self.assertEqual(provider["provider"]["kind"], "microsoft-vc-runtime")
        self.assertEqual(provider["source_package"], "CPython")
        self.assertEqual(provider["distribution_provenance"]["name"], "CPython")
        self.assertEqual(provider["notice_status"], "not-collected")
        self.assertEqual(provider["noticefiles"], [])
        self.assertEqual(unknown["provider"]["kind"], "unresolved")
        self.assertEqual(unknown_jvm["provider"]["kind"], "unresolved")

    def test_host_dpkg_attribution_copies_copyright_and_referenced_common_license(self):
        docs = self.root / "docs"
        package_docs = docs / "libsample1"
        licenses = self.root / "common-licenses"
        package_docs.mkdir(parents=True)
        licenses.mkdir()
        copyright_text = b"Package copyright\nLicense: GPL-3+\n See /usr/share/common-licenses/GPL-3\n"
        license_text = b"Exact common GPL license text\n"
        (package_docs / "copyright").write_bytes(copyright_text)
        (licenses / "GPL-3").write_bytes(license_text)
        with patch.object(native_report, "SYSTEM_DOCS_ROOT", docs), \
             patch.object(native_report, "COMMON_LICENSE_ROOT", licenses), \
             patch.object(native_report, "_package_for_path", return_value="libsample1:amd64"), \
             patch.object(native_report.subprocess, "run", return_value=subprocess_result(
                 "libsample1:amd64\t2.4.1+b1\tsample-source\t2.4.1\n"
             )) as run:
            evidence = native_report._host_provider(self.root / "libsample.so", self.output, {})
        self.assertEqual(run.call_args.args[0][0], "dpkg-query")
        self.assertEqual(
            run.call_args.args[0][2],
            "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n",
        )
        self.assertEqual(evidence["provider"], {"name": "libsample1:amd64", "version": "2.4.1+b1", "kind": "system-package"})
        self.assertEqual(evidence["source_package"], "sample-source")
        self.assertEqual(evidence["source_version"], "2.4.1")
        self.assertEqual(len(evidence["noticefiles"]), 2)
        for notice, expected in zip(evidence["noticefiles"], (copyright_text, license_text)):
            notice_path = self.output / notice["path"]
            self.assertEqual(notice_path.read_bytes(), expected)
            self.assertEqual(notice["sha256"], hashlib.sha256(expected).hexdigest())

    def test_ffmpeg_child_keeps_initial_failure_and_records_separate_retry_after_avformat(self):
        names = (
            "libavutil.so.60", "libavcodec.so.60", "libavformat.so.60",
            "libswresample.so.5", "libswscale.so.7",
        )
        for name in names:
            (self.bundle / name).write_bytes(b"fake native library")
        (self.bundle / "NovaMicSwitch.exe").write_bytes(b"do not load executable")
        load_counts = {}
        loaded_paths = []

        class FakeFunction:
            def __init__(self, name):
                self.name = name
                self.argtypes = None
                self.restype = None

            def __call__(self):
                if self.name.endswith("_version"):
                    return (60 << 16) | (2 << 8) | 4
                if self.name.endswith("_configuration"):
                    return b"--enable-gpl --enable-version3"
                return b"GPL version 3 or later"

        functions = []

        class FakeLibrary:
            def __init__(self, prefix):
                self.prefix = prefix

            def __getattr__(self, name):
                prefix = name.removesuffix("_version").removesuffix("_configuration").removesuffix("_license")
                function = FakeFunction(name)
                functions.append(function)
                return function

        def fake_cdll(path):
            name = Path(path).name
            loaded_paths.append(name)
            prefix = next((key for key in native_report.FFMPEG_LIBRARIES if name.lower().startswith((key, f"lib{key}"))), "")
            load_counts[prefix] = load_counts.get(prefix, 0) + 1
            if prefix in {"avutil", "avcodec"} and load_counts[prefix] == 1:
                raise OSError(f"initial {prefix} dependency unavailable")
            return FakeLibrary(prefix)

        libraries = [
            {"prefix": next(key for key in native_report.FFMPEG_LIBRARIES if name.lower().startswith((key, f"lib{key}"))),
             "path": str((self.bundle / name).resolve())}
            for name in names
        ]
        with patch.object(native_report.ctypes, "CDLL", side_effect=fake_cdll):
            response = native_report._ffmpeg_probe_child({"bundle": str(self.bundle), "libraries": libraries})
        evidence = {item["relative_path"]: item for item in response["results"]}
        util = evidence["libavutil.so.60"]
        codec = evidence["libavcodec.so.60"]
        self.assertEqual(util["load_error"], "OSError: initial avutil dependency unavailable")
        self.assertEqual(codec["load_error"], "OSError: initial avcodec dependency unavailable")
        self.assertIsNone(util["retry_after_avformat"]["load_error"])
        self.assertIsNone(codec["retry_after_avformat"]["load_error"])
        self.assertEqual(util["retry_after_avformat"]["version"], "60.2.4")
        self.assertEqual(codec["retry_after_avformat"]["version"], "60.2.4")
        self.assertEqual(evidence["libavformat.so.60"]["version"], "60.2.4")
        self.assertEqual(len(loaded_paths), 7)
        self.assertEqual(len(functions), 15)
        self.assertTrue(all(function.argtypes == [] for function in functions))

    def test_ffmpeg_probe_subprocess_success_timeout_bad_json_and_child_failure(self):
        path = self.bundle / "libavutil.so.60"
        path.write_bytes(b"not loaded by the parent")
        entry = {"prefix": "avutil", "path": str(path.resolve())}
        success = subprocess.CompletedProcess(
            ["python", "native_report.py"], 0,
            json.dumps({"results": [{"relative_path": "libavutil.so.60", "probe_kind": "raw_library_probe"}]}), "",
        )
        with patch.object(native_report.subprocess, "run", return_value=success) as run:
            result = native_report._ffmpeg_probe_subprocess(self.bundle, [entry])
        self.assertEqual(result["results"][0]["relative_path"], "libavutil.so.60")
        self.assertEqual(run.call_args.args[0][-1], "--_probe-ffmpeg")
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(entry["path"], json.loads(run.call_args.kwargs["input"])["libraries"][0]["path"])

        with patch.object(native_report.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            timeout = native_report._ffmpeg_probe_subprocess(self.bundle, [entry])
            evidence = native_report._ffmpeg_evidence(self.bundle)
        self.assertIn("TimeoutExpired", timeout["error"])
        self.assertIn("TimeoutExpired", evidence[0]["load_error"])
        malformed = subprocess.CompletedProcess(["python"], 0, "not json", "")
        with patch.object(native_report.subprocess, "run", return_value=malformed):
            invalid = native_report._ffmpeg_probe_subprocess(self.bundle, [entry])
        self.assertIn("malformed JSON", invalid["error"])
        failed = subprocess.CompletedProcess(["python"], 2, "", "child failed")
        with patch.object(native_report.subprocess, "run", return_value=failed):
            child_error = native_report._ffmpeg_probe_subprocess(self.bundle, [entry])
        self.assertIn("status 2", child_error["error"])


def subprocess_result(stdout):
    import subprocess
    return subprocess.CompletedProcess(["dpkg-query"], 0, stdout, "")


if __name__ == "__main__":
    unittest.main()
