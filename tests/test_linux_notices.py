import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect_linux_notices as collector


class LinuxNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.system = self.root / "usr" / "lib"
        self.system.mkdir(parents=True)
        self.docroot = self.root / "doc"
        self.commonroot = self.root / "common"
        self.docroot.mkdir()
        self.commonroot.mkdir()

        original_is_file = Path.is_file

        def posix_exact_common_is_file(path):
            try:
                relative = path.relative_to(self.commonroot)
            except ValueError:
                return original_is_file(path)
            if len(relative.parts) != 1:
                return original_is_file(path)
            exact_candidate = next((candidate for candidate in path.parent.iterdir()
                                    if candidate.name == path.name), None)
            return exact_candidate is not None and original_is_file(exact_candidate)

        exact_common_patch = patch.object(Path, "is_file", autospec=True, side_effect=posix_exact_common_is_file)
        exact_common_patch.start()
        self.addCleanup(exact_common_patch.stop)
        self.package = "libfixture1:amd64"
        self.copyright_bytes = (
            b"Fixture copyright text\nLicense: GPL-2+\nSee /usr/share/common-licenses/GPL-2\n"
        )
        self.license_bytes = b"Verbatim common license text\n"
        (self.docroot / "libfixture1").mkdir()
        (self.docroot / "libfixture1" / "copyright").write_bytes(self.copyright_bytes)
        (self.commonroot / "GPL-2").write_bytes(self.license_bytes)
        self.lib1 = self.system / "libfixture.so.1"
        self.lib2 = self.system / "libfixture.so.2"
        self.lib1.write_bytes(b"native bytes one")
        self.lib2.write_bytes(b"native bytes two")
        system_prefix_patch = patch.object(collector, "SYSTEM_PREFIXES", (self.system.as_posix() + "/",))
        system_prefix_patch.start()
        self.addCleanup(system_prefix_patch.stop)
        self.out = self.root / "output"
        self.out.mkdir()
        self.original_host = collector.native_report._host_provider

        def host_provider(source, output_dir, notice_cache):
            notice_dir = Path(output_dir) / "notices" / "package"
            notice_dir.mkdir(parents=True, exist_ok=True)
            entries = []
            contents = [("libfixture1_amd64-copyright.txt", self.copyright_bytes)]
            for reference in sorted(set(collector.NOTICE_REFERENCE.findall(self.copyright_bytes.decode("utf-8")))):
                common = next((candidate for candidate in self.commonroot.iterdir()
                               if candidate.name == reference and candidate.is_file()), None)
                if common is not None:
                    safe_reference = collector.native_report._safe_notice_name(reference)
                    contents.append((f"libfixture1_amd64-common-{safe_reference}.txt", common.read_bytes()))
            for filename, content in contents:
                destination = notice_dir / filename
                if not destination.exists():
                    destination.write_bytes(content)
                entries.append({"path": f"notices/package/{filename}", "sha256": hashlib.sha256(content).hexdigest()})
            notice_cache[self.package] = entries
            return {
                "provider": {"name": self.package, "version": "1:2.4-3", "kind": "system-package"},
                "source_package": "fixture-src", "source_version": "2:2.4-3ubuntu1",
                "noticefiles": entries, "notice_status": "copied", "resolution_detail": None,
            }

        self.host_provider = host_provider
        self.patches = [
            patch.object(collector.native_report, "_host_provider", side_effect=host_provider),
            patch.object(collector.native_report, "SYSTEM_DOCS_ROOT", self.docroot),
            patch.object(collector.native_report, "COMMON_LICENSE_ROOT", self.commonroot),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_deduplicates_package_and_copies_verbatim_text_not_native_inputs(self):
        result = collector.collect_linux_notices([
            ("lib/libfixture.so.1", str(self.lib1), "BINARY"),
            ("lib/libfixture.so.2", str(self.lib2), "EXTENSION"),
        ], self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(len(manifest["source_packages"]), 1)
        self.assertEqual(manifest["source_packages"][0]["version"], "2:2.4-3ubuntu1")
        self.assertEqual(manifest["binary_providers"][0]["provider"]["version"], "1:2.4-3")
        self.assertEqual(len(manifest["native_inputs"]), 2)
        text_entries = {item["path"]: item for item in manifest["texts"]}
        self.assertEqual(len(text_entries), 2)
        for item in text_entries.values():
            data = (self.out / item["path"]).read_bytes()
            expected = self.license_bytes if "common-" in item["path"] else self.copyright_bytes
            self.assertEqual(data, expected)
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
        self.assertEqual(self.lib1.read_bytes(), b"native bytes one")
        self.assertEqual(self.lib2.read_bytes(), b"native bytes two")
        self.assertEqual(len(result["files"]), 4)
        self.assertTrue((self.out / "THIRD-PARTY-NOTICES.md").exists())

    def test_missing_owner_fails_closed(self):
        with patch.object(collector.native_report, "_host_provider", return_value=None):
            with self.assertRaises(collector.LinuxNoticeError):
                collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_missing_copyright_fails_closed(self):
        def missing(*args):
            return {"provider": {"name": self.package, "version": "1", "kind": "system-package"}, "source_package": "src", "source_version": "1", "noticefiles": [], "notice_status": "missing"}
        with patch.object(collector.native_report, "_host_provider", side_effect=missing):
            with self.assertRaises(collector.LinuxNoticeError):
                collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_missing_referenced_common_license_fails_closed(self):
        (self.commonroot / "GPL-2").unlink()
        with self.assertRaises(collector.LinuxNoticeError):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_wrong_common_license_hash_fails_closed(self):
        def wrong_hash(source, stage, cache):
            entries = self.host_provider(source, stage, cache)
            entries["noticefiles"][1]["sha256"] = "0" * 64
            return entries
        with patch.object(collector.native_report, "_host_provider", side_effect=wrong_hash):
            with self.assertRaises(collector.LinuxNoticeError):
                collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_unsafe_toc_refused(self):
        with self.assertRaises(collector.LinuxNoticeError):
            collector.collect_linux_notices([("../escape", str(self.lib1), "BINARY")], self.out)

    def test_output_symlink_refused(self):
        link = self.root / "output-link"
        try:
            link.symlink_to(self.out, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are not supported on this platform")
        with self.assertRaises(collector.LinuxNoticeError):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], link)

    def test_foreign_files_retained_and_manifest_deterministic(self):
        foreign = self.out / "keep.txt"
        foreign.write_text("caller-owned")
        toc = [("lib/z.so", str(self.lib2), "BINARY"), ("lib/a.so", str(self.lib1), "EXECUTABLE")]
        # Separate outputs make deterministic ordering observable without overwriting prior output.
        first = collector.collect_linux_notices(toc, self.out)
        first_bytes = Path(first["manifest"]).read_bytes()
        other = self.root / "other"
        other.mkdir()
        second = collector.collect_linux_notices(toc, other)
        self.assertEqual(first_bytes, Path(second["manifest"]).read_bytes())
        self.assertEqual(foreign.read_text(), "caller-owned")

    def test_non_system_native_sources_are_scope_skips(self):
        result = collector.collect_linux_notices([("lib/libwheel.so", "/opt/wheel/libwheel.so", "BINARY")], self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["native_inputs"], [])
        self.assertEqual(len(manifest["scope_skips"]), 1)

    def test_repeat_is_validated_cache_hit(self):
        toc = [("lib/x.so", str(self.lib1), "BINARY")]
        first = collector.collect_linux_notices(toc, self.out)
        before = Path(first["manifest"]).read_bytes()
        second = collector.collect_linux_notices(toc, self.out)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(before, Path(second["manifest"]).read_bytes())

    def test_changed_toc_and_notice_regenerate_owned_files(self):
        first = collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        first_bytes = Path(first["manifest"]).read_bytes()
        foreign = self.out / "foreign.txt"
        foreign.write_text("keep")
        changed_copyright = b"Updated text\nSee /usr/share/common-licenses/GPL-2\n"
        self.copyright_bytes = changed_copyright
        (self.docroot / "libfixture1" / "copyright").write_bytes(changed_copyright)
        self.lib1.write_bytes(b"changed native input")
        second = collector.collect_linux_notices([("lib/renamed.so", str(self.lib1), "BINARY")], self.out)
        self.assertFalse(second["cache_hit"])
        manifest = json.loads(Path(second["manifest"]).read_text())
        self.assertEqual(manifest["native_inputs"][0]["destination"], "lib/renamed.so")
        self.assertEqual((self.out / foreign.name).read_text(), "keep")
        self.assertNotEqual(first_bytes, Path(second["manifest"]).read_bytes())

    def test_tampered_owned_file_refused(self):
        result = collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        text_path = self.out / manifest["texts"][0]["path"]
        text_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(collector.LinuxNoticeError, "tampered"):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_foreign_generated_path_collision_refused(self):
        foreign = self.out / "THIRD-PARTY-NOTICES.md"
        foreign.write_text("caller-owned")
        with self.assertRaisesRegex(collector.LinuxNoticeError, "unowned"):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        self.assertEqual(foreign.read_text(), "caller-owned")

    def test_common_license_reference_period_and_known_aliases(self):
        self.copyright_bytes = b"See /usr/share/common-licenses/GPL-2.0 and /usr/share/common-licenses/GPL-3.0. Also /usr/share/common-licenses/LGPL-2.1.\n"
        (self.docroot / "libfixture1" / "copyright").write_bytes(self.copyright_bytes)
        (self.commonroot / "GPL-3").write_bytes(b"GPL 3 canonical text")
        (self.commonroot / "LGPL-2.1").write_bytes(b"LGPL 2.1 canonical text")
        self.assertTrue((self.commonroot / "LGPL-2.1").is_file())
        self.assertFalse((self.commonroot / "LGPL-2.1.").is_file())
        result = collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        refs = {tuple(item.get("references", [])) for item in manifest["texts"] if "common-" in item["path"]}
        self.assertEqual(refs, {("GPL-2", "GPL-2.0"), ("GPL-3", "GPL-3.0."), ("LGPL-2.1", "LGPL-2.1.")})

    def test_unknown_common_alias_fails_closed(self):
        self.copyright_bytes = b"See /usr/share/common-licenses/GPL-2.2.\n"
        (self.docroot / "libfixture1" / "copyright").write_bytes(self.copyright_bytes)
        with self.assertRaisesRegex(collector.LinuxNoticeError, "unavailable"):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_missing_actual_common_file_fails_closed(self):
        (self.commonroot / "GPL-2").unlink()
        with self.assertRaisesRegex(collector.LinuxNoticeError, "unavailable"):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_common_license_symlink_within_approved_root_is_accepted(self):
        versioned = self.commonroot / "GPL-3-versioned"
        versioned.write_bytes(b"versioned GPL 3 text")
        link = self.commonroot / "GPL-3"
        try:
            link.symlink_to(versioned)
        except (OSError, NotImplementedError):
            self.skipTest("File symlinks are unavailable on this platform")
        self.copyright_bytes = b"See /usr/share/common-licenses/GPL-3.\n"
        (self.docroot / "libfixture1" / "copyright").write_bytes(self.copyright_bytes)
        result = collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        text = next(item for item in manifest["texts"] if "common-" in item["path"])
        self.assertEqual((self.out / text["path"]).read_bytes(), versioned.read_bytes())
        self.assertEqual(Path(text["resolved_source"]), versioned.resolve())

    def test_common_license_symlink_outside_approved_root_is_rejected(self):
        outside = self.root / "outside-license"
        outside.write_bytes(b"not under common-license root")
        try:
            (self.commonroot / "GPL-3").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("File symlinks are unavailable on this platform")
        self.copyright_bytes = b"See /usr/share/common-licenses/GPL-3.\n"
        (self.docroot / "libfixture1" / "copyright").write_bytes(self.copyright_bytes)
        with self.assertRaisesRegex(collector.LinuxNoticeError, "approved root"):
            collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)

    def test_period_and_plain_reference_share_notice_with_merged_provenance(self):
        self.copyright_bytes = b"See /usr/share/common-licenses/GPL-2. and /usr/share/common-licenses/GPL-2\n"
        (self.docroot / "libfixture1" / "copyright").write_bytes(self.copyright_bytes)
        result = collector.collect_linux_notices([("lib/x.so", str(self.lib1), "BINARY")], self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        common_texts = [item for item in manifest["texts"] if "common-" in item["path"]]
        self.assertEqual(len(common_texts), 1)
        self.assertEqual(common_texts[0]["references"], ["GPL-2", "GPL-2."])


if __name__ == "__main__":
    unittest.main()
