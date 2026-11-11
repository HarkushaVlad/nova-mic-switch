"""Offline tests for license collection and curated Qt text validation."""

from __future__ import annotations

import hashlib
import importlib.util
import importlib.metadata
import json
import os
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
        self.assertEqual((output / "ffmpeg-9.0.1/qt_attribution.json").read_bytes(),
                         (ROOT / "packaging/licenses/ffmpeg-9.0.1/qt_attribution.json").read_bytes())
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

    def test_qt_version_mismatch_fails_before_writing(self):
        self.fake_dists["PySide6_Addons"] = FakeDistribution("PySide6_Addons", "6.12.1")
        output = self.root / "bad-version"
        with self.assertRaisesRegex(collector.LicenseCollectionError, "does not match curated Qt licenses"):
            self.collect(output)
        self.assertFalse(output.exists())

    def test_missing_curated_text_fails(self):
        source = ROOT / "packaging/licenses/qt-6.12.0/LGPL-3.0-only.txt"
        original = source.read_bytes()
        try:
            source.write_bytes(b"")
            with self.assertRaisesRegex(collector.LicenseCollectionError, "hash mismatch"):
                self.collect()
        finally:
            source.write_bytes(original)

    def test_manifest_hash_mismatch_fails(self):
        source = ROOT / "packaging/licenses/qt-6.12.0/manifest.json"
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
        source = ROOT / "packaging/licenses/qt-6.12.0/manifest.json"
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
        source = ROOT / "packaging/licenses/ffmpeg-9.0.1/manifest.json"
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
        source = ROOT / "packaging/licenses/ffmpeg-9.0.1/LICENSE.MIT.txt"
        original = source.read_bytes()
        try:
            source.unlink()
            with self.assertRaisesRegex(collector.LicenseCollectionError, "Missing curated ffmpeg-9.0.1 license text"):
                self.collect()
        finally:
            source.write_bytes(original)

    def test_ffmpeg_manifest_hash_mismatch_fails(self):
        source = ROOT / "packaging/licenses/ffmpeg-9.0.1/manifest.json"
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
        with patch.object(importlib.metadata, "version", side_effect=lambda name: versions[name]):
            collector.ensure_licenses(output, distributions=self.fake_dists, python_license=self.python_text)
        manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
        self.assertIn("ffmpeg-9.0.1/qt_attribution.json", {item["destination"] for item in manifest["texts"]})
        self.assertEqual((output / "Python-LICENSE.txt").read_bytes(), self.python_text.read_bytes())


if __name__ == "__main__":
    unittest.main()
