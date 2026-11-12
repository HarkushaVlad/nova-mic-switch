import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
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

    def test_ffmpeg_probe_loads_only_named_bundle_libraries_and_records_functions(self):
        names = (
            "libavutil.so.60", "libavcodec.so.60", "libavformat.so.60",
            "libswresample.so.5", "libswscale.so.7",
        )
        for name in names:
            (self.bundle / name).write_bytes(b"fake native library")
        (self.bundle / "NovaMicSwitch.exe").write_bytes(b"do not load executable")
        loaded = []

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
            def __getattr__(self, name):
                function = FakeFunction(name)
                functions.append(function)
                return function

        def fake_cdll(path):
            loaded.append(Path(path).name)
            return FakeLibrary()

        with patch.object(native_report.ctypes, "CDLL", side_effect=fake_cdll):
            evidence = native_report._ffmpeg_evidence(self.bundle)
        self.assertEqual(sorted(loaded), sorted(names))
        self.assertEqual(len(evidence), 5)
        self.assertTrue(all(item["version"] == "60.2.4" for item in evidence))
        self.assertTrue(all(item["configuration"].startswith("--enable-gpl") for item in evidence))
        self.assertTrue(all(item["license"] == "GPL version 3 or later" for item in evidence))
        self.assertEqual(len(functions), 15)
        self.assertTrue(all(function.argtypes == [] for function in functions))


def subprocess_result(stdout):
    import subprocess
    return subprocess.CompletedProcess(["dpkg-query"], 0, stdout, "")


if __name__ == "__main__":
    unittest.main()
