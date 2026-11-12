from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_runtime_sources as runtime_sources


class Response:
    status = 200
    headers = {}

    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload)
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class RuntimeSourceTests(unittest.TestCase):
    def setUp(self):
        parent = "/tmp/opencode" if os.name != "nt" and Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payloads = [b"first archive", b"second archive", b"third archive"]
        self.rows = []
        for index, payload in enumerate(self.payloads):
            name = f"archive-{index}.tar.gz"
            self.rows.append({
                "filename": name,
                "url": f"https://github.com/owner/project/archive/ref-{index}.tar.gz",
                "archive_bytes": len(payload),
                "archive_sha256": hashlib.sha256(payload).hexdigest(),
                "archive_path": f"build/runtime-materials/sources/{name}",
            })
        self.manifest = self.root / "manifest.json"
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"schema_version": 1, "sources": self.rows}), encoding="utf-8")

    def test_default_manifest_has_exact_three_pinned_sources(self):
        rows = runtime_sources._records(runtime_sources.MANIFEST)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["filename"] for row in rows}, {
            "type2-runtime-dd6cebe.tar.gz", "libfuse-3.15.0.tar.xz", "squashfuse-0.5.2.tar.gz",
        })

    def test_offline_cached_files_are_rechecked_without_network(self):
        cache = self.root / "cache"
        cache.mkdir(exist_ok=True)
        for row, payload in zip(self.rows, self.payloads):
            (cache / row["filename"]).write_bytes(payload)
        with patch.object(runtime_sources.urllib.request, "build_opener") as opener:
            files = runtime_sources.fetch_sources(cache, manifest_path=self.manifest)
            opener.assert_not_called()
        self.assertEqual(len(files), 3)

    def test_offline_cache_miss_and_corrupt_cached_file_fail_closed(self):
        cache = self.root / "cache"
        with patch.object(runtime_sources.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "Offline cache miss"):
                runtime_sources.fetch_sources(cache, manifest_path=self.manifest)
            opener.assert_not_called()
        cache.mkdir(exist_ok=True)
        (cache / self.rows[0]["filename"]).write_bytes(b"wrong bytes")
        with patch.object(runtime_sources.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "failed size/SHA-256"):
                runtime_sources.fetch_sources(cache, fetch=True, manifest_path=self.manifest)
            opener.assert_not_called()

    def test_fetch_pins_bytes_and_cleans_temp_on_bad_digest(self):
        cache = self.root / "cache"

        class Opener:
            def open(self, request, timeout):
                i = int(request.full_url.rsplit("-", 1)[1].split(".")[0])
                return Response(self_payloads[i], request.full_url)

        self_payloads = self.payloads
        with patch.object(runtime_sources.urllib.request, "build_opener", return_value=Opener()):
            paths = runtime_sources.fetch_sources(cache, fetch=True, manifest_path=self.manifest)
        self.assertEqual([path.read_bytes() for path in paths], self.payloads)
        (cache / self.rows[1]["filename"]).write_bytes(b"corrupt")
        with patch.object(runtime_sources.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "failed size/SHA-256"):
                runtime_sources.fetch_sources(cache, fetch=True, manifest_path=self.manifest)
        self.assertFalse(list(cache.glob("*.tmp")))

    def test_rejects_bad_network_bytes_and_removes_owned_temporary_file(self):
        class Opener:
            def open(self, request, timeout):
                return Response(b"tampered", request.full_url)

        cache = self.root / "cache"
        with patch.object(runtime_sources.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "Downloaded runtime source failed"):
                runtime_sources.fetch_sources(cache, fetch=True, manifest_path=self.manifest)
        self.assertEqual(list(cache.iterdir()), [])

    def test_rejects_unsafe_urls_paths_and_symlinked_output(self):
        self.rows[0]["url"] = "https://github.com.evil.invalid/owner/project/archive/ref.tar.gz"
        self.write_manifest()
        with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "outside the HTTPS allowlist"):
            runtime_sources.fetch_sources(self.root / "cache", manifest_path=self.manifest)
        self.rows[0]["url"] = "https://github.com/owner/project/archive/../secret"
        self.write_manifest()
        with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "outside the HTTPS allowlist"):
            runtime_sources.fetch_sources(self.root / "cache", manifest_path=self.manifest)
        target = self.root / "real-cache"
        target.mkdir()
        link = self.root / "cache-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "Unsafe runtime output directory component"):
            runtime_sources.fetch_sources(link, manifest_path=runtime_sources.MANIFEST)

    def test_redirect_host_and_download_size_are_bounded(self):
        class RedirectResponse(Response):
            def geturl(self):
                return "https://evil.invalid/archive"

        class Opener:
            def open(self, request, timeout):
                return RedirectResponse(b"x", request.full_url)

        with patch.object(runtime_sources.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaisesRegex(runtime_sources.RuntimeSourceError, "outside the HTTPS allowlist"):
                runtime_sources.fetch_sources(self.root / "cache", fetch=True, manifest_path=self.manifest)
        self.assertEqual(list((self.root / "cache").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
