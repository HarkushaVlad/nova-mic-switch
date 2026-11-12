"""Offline tests for license collection and curated Qt text validation."""

from __future__ import annotations

import hashlib
import importlib.util
import importlib.metadata
import json
import os
import shutil
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_DIR = Path("/tmp/opencode") if os.name != "nt" and Path("/tmp/opencode").is_dir() else None
SPEC = importlib.util.spec_from_file_location("license_collector_test", ROOT / "scripts/collect_licenses.py")
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class FakeDistribution:
    def __init__(self, name, version="1.2.3", files=None):
        self.metadata = Message()
        self.metadata["Name"] = name
        self.metadata["License"] = "Example license declaration"
        self.version = version
        self.files = files or []

    def locate_file(self, item):
        return item


class LicenseCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nova-license-tests-", dir=TEST_TEMP_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture_root = self.root / "repository"
        for relative in (
            "packaging/licenses/qt-6.12.0", "packaging/licenses/ffmpeg-9.0.1",
            "packaging/licenses/qt-native", "packaging/licenses/appimage-runtime",
            "packaging/licenses/windows-native",
        ):
            shutil.copytree(ROOT / relative, self.fixture_root / relative)
        (self.fixture_root / "docs").mkdir(parents=True)
        shutil.copy2(ROOT / "LICENSE", self.fixture_root / "LICENSE")
        shutil.copy2(ROOT / "docs/LIBRARY-REPLACEMENT.md", self.fixture_root / "docs/LIBRARY-REPLACEMENT.md")
        self.fake_dists = {name: FakeDistribution(name) for name in collector.COLLECT_DISTRIBUTIONS}
        for name in collector.QT_DISTRIBUTIONS:
            self.fake_dists[name] = FakeDistribution(name, collector.QT_VERSION)
        self.python_text = self.root / "python-LICENSE.txt"
        self.python_text.write_text("Actual Python license fixture\n", encoding="utf-8")
        self.assets = {}
        for name, filename, content in (
            ("hidapi", "LICENSE", b"hidapi license text"),
            ("hidapi", "LICENSE-MIT", b"hidapi MIT alternative"),
            ("hidapi", "LICENSE-GPL", b"hidapi GPL alternative"),
            ("pyinstaller", "COPYING.txt", b"PyInstaller COPYING text"),
        ):
            path = self.root / name / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.assets[name, filename] = path
        self.fake_dists["hidapi"].files = [self.assets["hidapi", n] for n in ("LICENSE", "LICENSE-MIT", "LICENSE-GPL")]
        self.fake_dists["pyinstaller"].files = [self.assets["pyinstaller", "COPYING.txt"]]

    def collect(self, out=None):
        with patch.object(collector, "ROOT", self.fixture_root):
            return collector.collect_licenses(
                out or self.root / "licenses", distributions=self.fake_dists, python_license=self.python_text
            )

    def test_collects_actual_texts_and_provenance_hashes(self):
        output = self.collect()
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        destinations = {item["destination"] for item in manifest["texts"]}
        self.assertIn("Python-LICENSE.txt", destinations)
        self.assertIn("qt-6.12.0/LGPL-3.0-only.txt", destinations)
        self.assertIn("ffmpeg-9.0.1/qt_attribution.json", destinations)
        self.assertIn("ffmpeg-9.0.1/LICENSE.LGPL-2.1-or-later.txt", destinations)
        self.assertIn("qt-native/qtbase/src/gui/painting/qt_attribution.json", destinations)
        self.assertIn("appimage-runtime/type2-runtime/LICENSE", destinations)
        self.assertIn("appimage-runtime/permissive/zlib/LICENSE", destinations)
        self.assertIn("qt-native/manifest.json", destinations)
        self.assertIn("appimage-runtime/manifest.json", destinations)
        self.assertIn("appimage-runtime/permissive/manifest.json", destinations)
        self.assertIn("LICENSE.txt", destinations)
        self.assertIn("LIBRARY-REPLACEMENT.md", destinations)
        self.assertEqual((output / "ffmpeg-9.0.1/qt_attribution.json").read_bytes(),
                         (self.fixture_root / "packaging/licenses/ffmpeg-9.0.1/qt_attribution.json").read_bytes())
        self.assertEqual(sum("hidapi-" in name for name in destinations), 3)
        self.assertTrue(any(name.endswith("/COPYING.txt") for name in destinations))
        self.assertEqual((output / "packages/hidapi-1.2.3/LICENSE-MIT").read_bytes(), b"hidapi MIT alternative")
        notice_text = (output / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), notice_text)
        for item in manifest["texts"]:
            data = (output / item["destination"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
        self.assertIn("not a full SBOM", manifest["review"])
        self.assertEqual(manifest["generated_files"], ["THIRD-PARTY-NOTICES.md", "manifest.json"])
        self.assertEqual(hashlib.sha256((output / "THIRD-PARTY-NOTICES.md").read_bytes()).hexdigest(),
                         manifest["notice_sha256"])
        self.assertNotIn("/mnt/", json.dumps(manifest))

    def test_material_output_is_deterministic_and_collection_is_offline(self):
        with patch.object(collector.urllib.request, "urlopen", side_effect=AssertionError("unexpected network access")):
            first = self.collect(self.root / "first")
            second = self.collect(self.root / "second")
        first_manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual([row["destination"] for row in first_manifest["texts"]],
                         sorted((row["destination"] for row in first_manifest["texts"]), key=str.casefold))
        for rel in ("qt-native/qtbase/src/gui/painting/qt_attribution.json",
                    "appimage-runtime/type2-runtime/LICENSE",
                    "appimage-runtime/permissive/musl/LICENSE"):
            self.assertEqual((first / rel).read_bytes(), (second / rel).read_bytes())

    def test_material_cache_revalidates_all_capture_manifests_and_project_documents(self):
        output = self.collect()
        initial = json.loads((output / "manifest.json").read_text(encoding="utf-8"))["input_snapshot_sha256"]
        material_manifests = (
            self.fixture_root / "packaging/licenses/qt-native/manifest.json",
            self.fixture_root / "packaging/licenses/appimage-runtime/manifest.json",
            self.fixture_root / "packaging/licenses/appimage-runtime/permissive/manifest.json",
        )
        with patch.object(collector, "ROOT", self.fixture_root), patch.object(
            collector.urllib.request, "urlopen", side_effect=AssertionError("unexpected network access")
        ):
            for index, source in enumerate(material_manifests):
                source.write_bytes(source.read_bytes() + b"\n")
                collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
                cached = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
                self.assertNotEqual(initial, cached["input_snapshot_sha256"])
                initial = cached["input_snapshot_sha256"]
                self.assertEqual((output / f"{('qt-native', 'appimage-runtime', 'appimage-runtime/permissive')[index]}/manifest.json").read_bytes(), source.read_bytes())
            for source, destination in ((self.fixture_root / "LICENSE", "LICENSE.txt"),
                                        (self.fixture_root / "docs/LIBRARY-REPLACEMENT.md", "LIBRARY-REPLACEMENT.md")):
                source.write_bytes(source.read_bytes() + b"\n")
                previous = initial
                collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
                cached = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
                self.assertNotEqual(previous, cached["input_snapshot_sha256"])
                initial = cached["input_snapshot_sha256"]
                self.assertEqual((output / destination).read_bytes(), source.read_bytes())

    def test_cache_revalidates_python_license_and_installed_distribution_versions(self):
        output = self.collect()
        manifest_path = output / "manifest.json"
        original_digest = json.loads(manifest_path.read_text(encoding="utf-8"))["input_snapshot_sha256"]
        with patch.object(collector, "ROOT", self.fixture_root), patch.object(
            collector.urllib.request, "urlopen", side_effect=AssertionError("unexpected network access")
        ):
            self.python_text.write_bytes(b"Updated Python license fixture\n")
            collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotEqual(original_digest, updated["input_snapshot_sha256"])
            self.assertEqual((output / "Python-LICENSE.txt").read_bytes(), self.python_text.read_bytes())

            self.fake_dists["hidapi"] = FakeDistribution("hidapi", "2.0.0", self.fake_dists["hidapi"].files)
            previous_digest = updated["input_snapshot_sha256"]
            collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotEqual(previous_digest, updated["input_snapshot_sha256"])
            self.assertEqual(next(row["version"] for row in updated["distributions"] if row["name"] == "hidapi"),
                             "2.0.0")

    def test_unchanged_cache_is_offline_and_does_not_recollect(self):
        output = self.collect()
        with patch.object(collector, "ROOT", self.fixture_root), patch.object(
            collector, "collect_licenses", side_effect=AssertionError("cache should be reused")
        ), patch.object(collector.urllib.request, "urlopen", side_effect=AssertionError("unexpected network access")):
            self.assertEqual(collector.ensure_licenses(output, distributions=self.fake_dists,
                                                        python_license=self.python_text), output)

    def test_old_cache_missing_added_groups_is_regenerated(self):
        output = self.collect()
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for destination in ("qt-native/manifest.json", "appimage-runtime/manifest.json",
                            "appimage-runtime/permissive/manifest.json", "LICENSE.txt", "LIBRARY-REPLACEMENT.md"):
            manifest["texts"] = [item for item in manifest["texts"] if item["destination"] != destination]
            (output / destination).unlink()
        manifest.pop("input_snapshot_sha256", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(collector, "ROOT", self.fixture_root):
            collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
        rebuilt = json.loads(manifest_path.read_text(encoding="utf-8"))
        destinations = {item["destination"] for item in rebuilt["texts"]}
        self.assertTrue({"qt-native/manifest.json", "appimage-runtime/manifest.json",
                         "appimage-runtime/permissive/manifest.json", "LICENSE.txt",
                         "LIBRARY-REPLACEMENT.md"}.issubset(destinations))

    def test_material_manifest_rejects_traversal_duplicate_empty_bad_hash_and_symlink(self):
        manifest_path = self.fixture_root / "packaging/licenses/qt-native/manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = (
            ("traversal", lambda doc: doc["copied_text_files"][0].__setitem__("destination", "packaging/licenses/qt-native/../escape")),
            ("duplicate", lambda doc: doc["copied_text_files"][1].__setitem__("destination", doc["copied_text_files"][0]["destination"])),
            ("bad hash", lambda doc: doc["copied_text_files"][0].__setitem__("sha256", "0" * 64)),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                document = json.loads(json.dumps(original))
                mutate(document)
                manifest_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(collector.LicenseCollectionError):
                    self.collect()
        manifest_path.write_text(json.dumps(original), encoding="utf-8")
        first = original["copied_text_files"][0]
        source = self.fixture_root / first["destination"]
        source.write_bytes(b"")
        with self.assertRaisesRegex(collector.LicenseCollectionError, "empty"):
            self.collect()
        source.write_bytes((ROOT / first["destination"]).read_bytes())
        if hasattr(source, "symlink_to") and os.name != "nt":
            target = self.root / "escape.txt"
            target.write_bytes(b"outside material")
            source.unlink()
            source.symlink_to(target)
            with self.assertRaisesRegex(collector.LicenseCollectionError, "Symlink"):
                self.collect()

    def test_windows_native_texts_preserve_manifest_provenance_and_roles(self):
        output = self.collect()
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        generated = {row["destination"]: row for row in manifest["texts"]}
        capture_path = self.fixture_root / "packaging/licenses/windows-native/manifest.json"
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        rows = capture["copied_text_files"]
        self.assertEqual(len(rows), 14)
        self.assertIn("windows-native/manifest.json", generated)
        self.assertEqual((output / "windows-native/manifest.json").read_bytes(), capture_path.read_bytes())
        for row in rows:
            destination = row["destination"].replace("packaging/licenses/windows-native/", "windows-native/", 1)
            entry = generated[destination]
            data = (output / destination).read_bytes()
            self.assertEqual(data, (self.fixture_root / row["destination"]).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
            self.assertEqual(entry["sha256"], row["sha256"])
            self.assertEqual(entry["size_bytes"], row["bytes"])
            self.assertEqual(entry["context"], row["component"])
            self.assertEqual(entry["source_url"], row["original_member_or_source_url"])
            self.assertEqual(entry["material_kind"], row["role"])
            self.assertTrue(entry["version"])
        notice = (output / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Windows native dependency source materials", notice)
        self.assertIn("not proof of source-to-binary reproducibility", notice)
        self.assertIn("does not relicense any third-party material", notice)
        self.assertIn("Microsoft Visual C++ redistributable inventory/terms remain pending", notice)

    def test_windows_native_source_safety_checks(self):
        manifest_path = self.fixture_root / "packaging/licenses/windows-native/manifest.json"
        original_bytes = manifest_path.read_bytes()
        original = json.loads(original_bytes)
        cases = (
            ("traversal", lambda doc: doc["copied_text_files"][0].__setitem__(
                "destination", "packaging/licenses/windows-native/../escape")),
            ("duplicate", lambda doc: doc["copied_text_files"][1].__setitem__(
                "destination", doc["copied_text_files"][0]["destination"])),
            ("bad hash", lambda doc: doc["copied_text_files"][0].__setitem__("sha256", "0" * 64)),
            ("bad size", lambda doc: doc["copied_text_files"][0].__setitem__("bytes", 0)),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                document = json.loads(json.dumps(original))
                mutate(document)
                manifest_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(collector.LicenseCollectionError):
                    self.collect()
        manifest_path.write_bytes(original_bytes)
        first = original["copied_text_files"][0]
        source = self.fixture_root / first["destination"]
        if os.name != "nt":
            target = self.root / "windows-native-escape.txt"
            target.write_bytes(b"outside material")
            source.unlink()
            source.symlink_to(target)
            with self.assertRaisesRegex(collector.LicenseCollectionError, "Symlink"):
                self.collect()

    def test_windows_native_capture_changes_invalidate_cache(self):
        output = self.collect()
        manifest_path = output / "manifest.json"
        initial = json.loads(manifest_path.read_text(encoding="utf-8"))["input_snapshot_sha256"]
        capture_path = self.fixture_root / "packaging/licenses/windows-native/manifest.json"
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        row = capture["copied_text_files"][0]
        source_path = self.fixture_root / row["destination"]
        updated_data = source_path.read_bytes() + b"\n"
        source_path.write_bytes(updated_data)
        row["bytes"] = len(updated_data)
        row["sha256"] = hashlib.sha256(updated_data).hexdigest()
        capture_path.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
        with patch.object(collector, "ROOT", self.fixture_root), patch.object(
            collector.urllib.request, "urlopen", side_effect=AssertionError("unexpected network access")
        ):
            collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
            rebuilt = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotEqual(initial, rebuilt["input_snapshot_sha256"])
        destination = row["destination"].replace("packaging/licenses/windows-native/", "windows-native/", 1)
        self.assertEqual((output / destination).read_bytes(), updated_data)
        self.assertEqual((output / "windows-native/manifest.json").read_bytes(), capture_path.read_bytes())

    def test_qt_version_mismatch_fails_before_writing(self):
        self.fake_dists["PySide6_Addons"] = FakeDistribution("PySide6_Addons", "6.12.1")
        output = self.root / "bad-version"
        with self.assertRaisesRegex(collector.LicenseCollectionError, "does not match curated Qt licenses"):
            self.collect(output)
        self.assertFalse(output.exists())

    def test_missing_curated_text_fails(self):
        source = self.fixture_root / "packaging/licenses/qt-6.12.0/LGPL-3.0-only.txt"
        original = source.read_bytes()
        try:
            source.write_bytes(b"")
            with self.assertRaisesRegex(collector.LicenseCollectionError, "hash mismatch"):
                self.collect()
        finally:
            source.write_bytes(original)

    def test_manifest_hash_mismatch_fails(self):
        source = self.fixture_root / "packaging/licenses/qt-6.12.0/manifest.json"
        original = source.read_text(encoding="utf-8")
        manifest = json.loads(original)
        manifest["licenses"][0]["sha256"] = "0" * 64
        try:
            source.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(collector.LicenseCollectionError, "hash mismatch"):
                self.collect()
        finally:
            source.write_text(original, encoding="utf-8")

    def test_manifest_path_traversal_fails(self):
        source = self.fixture_root / "packaging/licenses/qt-6.12.0/manifest.json"
        original = source.read_text(encoding="utf-8")
        manifest = json.loads(original)
        manifest["licenses"][0]["filename"] = "../GPL-3.0-only.txt"
        try:
            source.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(collector.LicenseCollectionError, "Unsafe or duplicate filename"):
                self.collect()
        finally:
            source.write_text(original, encoding="utf-8")

    def test_malformed_manifest_root_is_a_collection_error(self):
        source = self.fixture_root / "packaging/licenses/ffmpeg-9.0.1/manifest.json"
        original = source.read_text(encoding="utf-8")
        try:
            source.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(collector.LicenseCollectionError, "Invalid curated ffmpeg-9.0.1 license manifest"):
                self.collect()
        finally:
            source.write_text(original, encoding="utf-8")

    def test_metadata_license_declaration_does_not_create_fake_text(self):
        output = self.collect()
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        qt_names = {row["name"]: row["notice_files"] for row in manifest["distributions"]}
        self.assertEqual(qt_names["PySide6"], [])
        self.assertFalse(any(path.name == "Example license declaration" for path in output.rglob("*")))

    def test_output_keeps_unrelated_existing_files(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "user-file.txt").write_text("preserve", encoding="utf-8")
        self.collect(output)
        self.assertEqual((output / "user-file.txt").read_text(encoding="utf-8"), "preserve")

    def test_missing_ffmpeg_text_fails(self):
        source = self.fixture_root / "packaging/licenses/ffmpeg-9.0.1/LICENSE.MIT.txt"
        original = source.read_bytes()
        try:
            source.unlink()
            with self.assertRaisesRegex(collector.LicenseCollectionError, "Missing curated ffmpeg-9.0.1 license text"):
                self.collect()
        finally:
            source.write_bytes(original)

    def test_ffmpeg_manifest_hash_mismatch_fails(self):
        source = self.fixture_root / "packaging/licenses/ffmpeg-9.0.1/manifest.json"
        original = source.read_text(encoding="utf-8")
        manifest = json.loads(original)
        manifest["licenses"][0]["sha256"] = "0" * 64
        try:
            source.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(collector.LicenseCollectionError, "hash mismatch"):
                self.collect()
        finally:
            source.write_text(original, encoding="utf-8")

    def test_old_cache_without_ffmpeg_is_regenerated(self):
        output = self.collect()
        cache_manifest_path = output / "manifest.json"
        cache_manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
        removed = next(item for item in cache_manifest["texts"]
                       if item["destination"] == "ffmpeg-9.0.1/qt_attribution.json")
        cache_manifest["texts"].remove(removed)
        cache_manifest_path.write_text(json.dumps(cache_manifest), encoding="utf-8")
        (output / removed["destination"]).unlink()
        versions = {name: distribution.version for name, distribution in self.fake_dists.items()}
        with patch.object(importlib.metadata, "version", side_effect=lambda name: versions[name]), patch.object(collector, "ROOT", self.fixture_root):
            collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
        manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
        self.assertIn("ffmpeg-9.0.1/qt_attribution.json", {item["destination"] for item in manifest["texts"]})
        self.assertEqual((output / "Python-LICENSE.txt").read_bytes(), self.python_text.read_bytes())


if __name__ == "__main__":
    unittest.main()
