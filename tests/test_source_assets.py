import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_source_assets as source_assets


class FakeResponse:
    def __init__(self, payload, *, url, declared_length=None):
        self.payload = payload
        self.offset = 0
        self.headers = {} if declared_length is None else {"Content-Length": str(declared_length)}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload)
        result = self.payload[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class SourceAssetTests(unittest.TestCase):
    def setUp(self):
        temp_root = "/tmp/opencode" if os.name != "nt" and Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads(source_assets.DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.config["qt_ffmpeg_recipes"]["resolved_commit"] = "c" * 40
        self.config["qt_ffmpeg_recipes"]["sha256"] = None
        for recipe in self.config["qt_ffmpeg_recipes"]["files"]:
            recipe["sha256"] = None
        self.config_path = self.root / "upstream.json"
        self.save_config()
        self.cache = self.root / "cache"
        self.output = self.root / "output"

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    @staticmethod
    def digest(payload):
        return hashlib.sha256(payload).hexdigest()

    def test_offline_default_fails_actionably_without_network_or_cache(self):
        with patch.object(source_assets, "_open_url") as opener:
            with self.assertRaisesRegex(source_assets.SourceAssetError, "Missing cached Qt recipe"):
                source_assets.stage_source_assets(self.cache, self.output, config_path=self.config_path)
            opener.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_explicit_fetch_verifies_and_indexes_assets_and_deterministic_recipe_subset(self):
        helper_archive = io.BytesIO()
        with tarfile.open(fileobj=helper_archive, mode="w:gz") as archive:
            for name in sorted(source_assets.QT5_REQUIRED_HELPER_NAMES):
                payload = ("helper " + name).encode()
                member = tarfile.TarInfo("qt5/coin/provisioning/" + name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        archive_payloads = {
            "qt-everywhere-src": b"qt archive source bytes",
            "pyside-setup": b"pyside commit archive bytes",
            "qt5-supermodule-source": helper_archive.getvalue(),
            "ffmpeg": b"ffmpeg tarball verified against recipe sha1",
        }
        qt_asset = next(entry for entry in self.config["assets"] if entry["id"] == "qt-everywhere-src")
        qt_asset["sha256"] = None
        qt_asset["size_bytes"] = len(archive_payloads["qt-everywhere-src"])
        recipe_payloads = {path: ("recipe bytes for " + path).encode() for path in source_assets.RECIPE_PATHS}
        ffmpeg_sha1 = hashlib.sha1(archive_payloads["ffmpeg"]).hexdigest()
        next(entry for entry in self.config["assets"] if entry["id"] == "ffmpeg")["sha1"] = ffmpeg_sha1
        self.save_config()
        expected = dict(source_assets.EXPECTED_ASSETS)
        expected["qt-everywhere-src"] = {
            **expected["qt-everywhere-src"], "sha256": None,
            "size_bytes": len(archive_payloads["qt-everywhere-src"]),
        }
        expected["ffmpeg"] = {**expected["ffmpeg"], "sha1": ffmpeg_sha1}

        qt_url = self.config["assets"][0]["url"]
        qt_checksum_url = qt_url + ".sha256"
        qt_checksum = f"{self.digest(archive_payloads['qt-everywhere-src'])}  {Path(qt_url).name}\n".encode()
        refs = {
            source_assets.QT_TAG_URL: json.dumps({"object": {
                "type": "tag", "sha": "41737d37054a9c500b2d0df38199593accb8b5c0",
            }}).encode(),
            "https://api.github.com/repos/qt/qt5/git/tags/41737d37054a9c500b2d0df38199593accb8b5c0":
                json.dumps({"object": {"type": "commit", "sha": "c" * 40}}).encode(),
        }

        def fake_open(url):
            if url == qt_checksum_url:
                return FakeResponse(qt_checksum, url=url, declared_length=len(qt_checksum))
            for asset in self.config["assets"]:
                if url == asset["url"]:
                    payload = archive_payloads[asset["id"]]
                    final_url = "https://codeload.github.com/pyside/pyside-setup/archive/commit.tar.gz" if asset["id"] == "pyside-setup" else url
                    if asset["id"] == "qt5-supermodule-source":
                        final_url = "https://codeload.github.com/qt/qt5/archive/commit.tar.gz"
                    return FakeResponse(payload, url=final_url, declared_length=len(payload))
            if url in refs:
                payload = refs[url]
                return FakeResponse(payload, url=url, declared_length=len(payload))
            for recipe_path, payload in recipe_payloads.items():
                if url == f"https://raw.githubusercontent.com/qt/qt5/{'c' * 40}/{recipe_path}":
                    return FakeResponse(payload, url=url, declared_length=len(payload))
            raise AssertionError(f"Unexpected network request: {url}")

        with patch.dict(source_assets.EXPECTED_ASSETS, expected), patch.object(source_assets, "_open_url", side_effect=fake_open):
            index = source_assets.stage_source_assets(self.cache, self.output, fetch=True, config_path=self.config_path)

        self.assertEqual(len(index), 5)
        indexed = {entry["asset_id"]: entry for entry in index}
        self.assertEqual(indexed["qt-everywhere-src"]["upstream_sha256_url"], qt_checksum_url)
        self.assertIn("official Qt checksum file", indexed["qt-everywhere-src"]["verification_basis"])
        self.assertEqual(indexed["ffmpeg"]["sha1"], ffmpeg_sha1)
        self.assertIn("not an upstream cryptographic attestation", indexed["ffmpeg"]["verification_basis"])
        self.assertEqual(indexed["qt5-ffmpeg-recipes"]["source_commit"], "c" * 40)
        for asset in self.config["assets"]:
            payload = archive_payloads[asset["id"]]
            self.assertEqual((self.output / asset["filename"]).read_bytes(), payload)
        recipe_path = self.output / "qt-6.12.0-ffmpeg-recipes.tar.gz"
        with tarfile.open(fileobj=gzip.GzipFile(fileobj=io.BytesIO(recipe_path.read_bytes())), mode="r:") as archive:
            members = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
        prefix = "qt5-ffmpeg-recipes-" + "c" * 40 + "/"
        for relative_recipe_path, payload in recipe_payloads.items():
            self.assertEqual(members[prefix + relative_recipe_path], payload)
        explanation = json.loads(members[prefix + "SOURCE-MATERIAL.json"])
        self.assertIn("not a complete Qt source/build environment", explanation["scope_note"])

        output_index = json.loads((self.output / "source-index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(output_index["assets"]), 5)
        self.assertTrue((self.output / "SOURCE-INDEX.md").is_file())
        checksum_lines = (self.output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
        self.assertEqual(len(checksum_lines), 5)
        for line in checksum_lines:
            digest, filename = line.split("  ", 1)
            self.assertEqual(self.digest((self.output / filename).read_bytes()), digest)

        recipe_hash = self.digest(recipe_path.read_bytes())
        with patch.dict(source_assets.EXPECTED_ASSETS, expected), patch.object(source_assets, "_open_url") as opener:
            second_index = source_assets.stage_source_assets(self.cache, self.output, fetch=False, config_path=self.config_path)
            opener.assert_not_called()
        self.assertEqual(self.digest((self.output / "qt-6.12.0-ffmpeg-recipes.tar.gz").read_bytes()), recipe_hash)
        self.assertEqual(indexed["qt5-ffmpeg-recipes"]["sha256"],
                         {entry["asset_id"]: entry for entry in second_index}["qt5-ffmpeg-recipes"]["sha256"])

    def test_cached_digest_mismatch_fails_without_refetch(self):
        self.cache.mkdir()
        first_asset = self.config["assets"][0]
        cached = self.cache / first_asset["filename"]
        cached.write_bytes(b"original archive")
        record = {"url": first_asset["url"], "sha256": self.digest(b"original archive"), "size_bytes": len(b"original archive")}
        (self.cache / "source-cache.json").write_text(json.dumps({"schema_version": 1, "assets": {
            "qt-everywhere-src": record,
        }}), encoding="utf-8")
        cached.write_bytes(b"tampered archive")
        with patch.object(source_assets, "_open_url") as opener:
            with self.assertRaisesRegex(source_assets.SourceAssetError, "failed its recorded"):
                source_assets._verified_cached_asset(first_asset, self.cache, fetch=True)
            opener.assert_not_called()

    def test_content_length_and_digest_mismatches_cleanup_temporary_files(self):
        target = self.cache / "archive.bin"
        payload = b"small immutable source bytes"
        self.cache.mkdir()
        with patch.object(source_assets, "_open_url", return_value=FakeResponse(payload, url="https://ffmpeg.org/file", declared_length=len(payload) + 1)):
            with self.assertRaisesRegex(source_assets.SourceAssetError, "Content-Length"):
                source_assets._download_file("https://ffmpeg.org/file", target, max_bytes=100)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.cache.iterdir()), [])
        with patch.object(source_assets, "_open_url", return_value=FakeResponse(payload, url="https://ffmpeg.org/file", declared_length=len(payload))):
            with self.assertRaisesRegex(source_assets.SourceAssetError, "SHA-256"):
                source_assets._download_file("https://ffmpeg.org/file", target, max_bytes=100,
                                             expected_sha256="0" * 64)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.cache.iterdir()), [])
        with patch.object(source_assets, "_open_url", return_value=FakeResponse(payload, url="https://ffmpeg.org/file", declared_length=len(payload))):
            with self.assertRaisesRegex(source_assets.SourceAssetError, "SHA-1"):
                source_assets._download_file("https://ffmpeg.org/file", target, max_bytes=100,
                                             expected_sha1="0" * 40)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_streaming_limit_rejects_oversize_and_cleans_owned_partial(self):
        target = self.cache / "oversize.bin"
        self.cache.mkdir()
        payload = b"x" * 32
        with patch.object(source_assets, "_open_url", return_value=FakeResponse(payload, url="https://ffmpeg.org/file")):
            with self.assertRaisesRegex(source_assets.SourceAssetError, "exceeds 8 bytes"):
                source_assets._download_file("https://ffmpeg.org/file", target, max_bytes=8)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_rejects_unapproved_schemes_hosts_and_redirects(self):
        for url in ("http://download.qt.io/archive", "https://example.org/archive", "https://user@github.com/path"):
            with self.subTest(url=url), self.assertRaises(source_assets.SourceAssetError):
                source_assets._validate_https_url(url)
        handler = source_assets._AllowlistedRedirectHandler()
        request = source_assets.urllib.request.Request("https://github.com/source/archive")
        with self.assertRaisesRegex(source_assets.SourceAssetError, "allowlist"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.invalid/archive")

    def test_qt_redirect_allows_only_exact_versioned_accum_mirror(self):
        request = source_assets.urllib.request.Request(source_assets.QT_CANONICAL_URL)
        handler = source_assets._AllowlistedRedirectHandler()
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {},
                                                      source_assets.QT_APPROVED_MIRROR_URL))
        response = FakeResponse(b"", url=source_assets.QT_APPROVED_MIRROR_URL)
        self.assertEqual(source_assets._response_url(response, source_assets.QT_CANONICAL_URL),
                         source_assets.QT_APPROVED_MIRROR_URL)
        mirror_request = source_assets.urllib.request.Request(source_assets.QT_APPROVED_MIRROR_URL)
        self.assertIsNotNone(handler.redirect_request(mirror_request, None, 302, "Found", {},
                                                      source_assets.QT_APPROVED_BACKEND_URL))
        self.assertEqual(source_assets._response_url(
            FakeResponse(b"", url=source_assets.QT_APPROVED_BACKEND_URL), source_assets.QT_CANONICAL_URL,
        ), source_assets.QT_APPROVED_BACKEND_URL)
        rejected = (
            "https://mirror.accum.se.evil/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz",
            "https://mirror.accum.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.1/single/qt-everywhere-src-6.12.0.tar.xz",
            "http://mirror.accum.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz",
            source_assets.QT_APPROVED_BACKEND_URL,
            "https://chuangtzu.ftp.acc.umu.se.evil/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz",
            "https://chuangtzu.ftp.acc.umu.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.1/single/qt-everywhere-src-6.12.0.tar.xz",
        )
        for target in rejected:
            with self.subTest(target=target), self.assertRaises(source_assets.SourceAssetError):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        with self.assertRaises(source_assets.SourceAssetError):
            handler.redirect_request(mirror_request, None, 302, "Found", {},
                                     "https://chuangtzu.ftp.acc.umu.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.1/single/qt-everywhere-src-6.12.0.tar.xz")

    def test_qt_unapproved_regional_redirect_retries_only_exact_approved_mirror(self):
        calls = []

        class FakeOpener:
            def open(self, request, timeout):
                calls.append((request.full_url, timeout))
                if len(calls) == 1:
                    raise source_assets.SourceAssetError(
                        "Qt source redirect is not the specifically approved mirror URL"
                    )
                return FakeResponse(b"approved mirror bytes", url=source_assets.QT_APPROVED_BACKEND_URL)

        with patch.object(source_assets.urllib.request, "build_opener", return_value=FakeOpener()):
            response = source_assets._open_url(source_assets.QT_CANONICAL_URL)
        self.assertEqual(response.read(), b"approved mirror bytes")
        self.assertEqual([url for url, _ in calls], [
            source_assets.QT_CANONICAL_URL, source_assets.QT_APPROVED_MIRROR_URL,
        ])
        self.assertTrue(all(timeout == source_assets.DOWNLOAD_TIMEOUT_SECONDS for _, timeout in calls))

    def test_qt_mirror_backend_redirect_is_exact_and_version_scoped(self):
        request = source_assets.urllib.request.Request(source_assets.QT_APPROVED_MIRROR_URL)
        handler = source_assets._AllowlistedRedirectHandler()
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {},
                                                      source_assets.QT_APPROVED_BACKEND_URL))
        for target in (
            "https://chuangtzu.ftp.acc.umu.se.evil/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz",
            "https://chuangtzu.ftp.acc.umu.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.1/single/qt-everywhere-src-6.12.0.tar.xz",
            "https://other.ftp.acc.umu.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz",
        ):
            with self.subTest(target=target), self.assertRaises(source_assets.SourceAssetError):
                handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_pyside_annotated_tag_object_is_rejected_for_qt5_tag(self):
        wrong = json.dumps({"object": {
            "type": "tag", "sha": "6aefae25747a8d860d201f3ab2b2e199b9e8e006",
        }}).encode()
        with patch.object(source_assets, "_read_small", return_value=(wrong, source_assets.QT_TAG_URL)):
            with self.assertRaisesRegex(source_assets.SourceAssetError, "annotated tag object"):
                source_assets._resolve_qt_commit("41737d37054a9c500b2d0df38199593accb8b5c0")

    def test_recipe_file_pins_and_archive_checksum_are_strict(self):
        self.config["qt_ffmpeg_recipes"]["files"][0]["sha256"] = "not-a-digest"
        self.save_config()
        with self.assertRaisesRegex(source_assets.SourceAssetError, "recipe entry"):
            source_assets._read_config(self.config_path)

    def test_cli_requires_explicit_cache_and_output_and_fetch_is_opt_in(self):
        with patch.object(source_assets, "stage_source_assets", return_value=[] ) as stage:
            self.assertEqual(source_assets.main(["--cache-dir", str(self.cache), "--output-dir", str(self.output)]), 0)
            self.assertFalse(stage.call_args.kwargs["fetch"])
            source_assets.main(["--cache-dir", str(self.cache), "--output-dir", str(self.output), "--fetch"])
            self.assertTrue(stage.call_args.kwargs["fetch"])


if __name__ == "__main__":
    unittest.main()
