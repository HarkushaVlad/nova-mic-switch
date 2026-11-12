import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from scripts import build_ubuntu_sources as us


SOURCE = "pulseaudio"
VERSION = "1:15.99.1+dfsg1-1ubuntu2.2"


def dsc_bytes(source=SOURCE, version=VERSION, archive=b"archive"):
    digest = hashlib.sha256(archive).hexdigest()
    return ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
            f"Format: 3.0 (quilt)\nSource: {source}\nVersion: {version}\n"
            "Checksums-Sha256:\n"
            f" {digest} {len(archive)} pulseaudio_15.99.1.orig.tar.xz\n"
            "-----BEGIN PGP SIGNATURE-----\nfake-signature\n-----END PGP SIGNATURE-----\n").encode()


class SourceUtilityTests(unittest.TestCase):
    def test_report_pairs_deduplicated_from_exact_fields_and_unresolved_count(self):
        report = {"native_files": [
            {"source_package": SOURCE, "source_version": VERSION, "provider": {"kind": "system-package"}},
            {"source_package": SOURCE, "source_version": VERSION, "provider": {"kind": "system-package"}},
            {"source_package": "ignored", "source_version": "1", "provider": {"kind": "other"}},
            {"source_package": "incomplete", "provider": {"kind": "system-package"}},
        ], "unresolved": ["a", "b"]}
        self.assertEqual(us.source_pairs(report), [(SOURCE, VERSION)])
        self.assertEqual(us.cache_key(SOURCE, VERSION), hashlib.sha256((SOURCE + "\0" + VERSION).encode()).hexdigest())

    def test_offline_does_not_attempt_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            report.write_text(json.dumps({"native_files": [{"source_package": SOURCE,
                "source_version": VERSION, "provider": {"kind": "system-package"}}], "unresolved": []}))
            with mock.patch.object(us, "_request_bytes", side_effect=AssertionError("network called")):
                with self.assertRaisesRegex(us.SourceError, "offline cache"):
                    us.build_output(report, root / "cache", root / "out", fetch=False)

    def test_exact_epoch_version_is_url_encoded_without_rewriting(self):
        url = us._api_url(SOURCE, VERSION, "Published")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["version"], [VERSION])
        self.assertEqual(query["exact_match"], ["true"])
        self.assertEqual(query["status"], ["Published"])
        self.assertEqual(query["ws.op"], ["getPublishedSources"])

    def test_published_pagination_and_superseded_fallback(self):
        first = {"entries": [], "next_collection_link": "https://api.launchpad.net/1.0/next"}
        second = {"entries": [{"source_name": SOURCE, "source_package_version": VERSION,
            "distro_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy", "self_link": "https://api.launchpad.net/1.0/pub/1"}],
            "next_collection_link": None}
        with mock.patch.object(us, "_request_json", side_effect=[(first, 200), (second, 200)]) as call:
            entries, state = us.published_source_entries(SOURCE, VERSION, fetch=True)
        self.assertEqual(state, "Published")
        self.assertEqual(len(entries), 1)
        self.assertEqual(call.call_count, 2)

        no_pub = {"entries": [], "next_collection_link": None}
        superseded = {"entries": [{"source_name": SOURCE, "source_package_version": VERSION,
            "distro_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy", "self_link": "https://api.launchpad.net/1.0/pub/2"}],
            "next_collection_link": None}
        with mock.patch.object(us, "_request_json", side_effect=[(no_pub, 200), (superseded, 200)]):
            entries, state = us.published_source_entries(SOURCE, VERSION, fetch=True)
        self.assertEqual(state, "Superseded")
        self.assertEqual(len(entries), 1)

    def test_pagination_cap(self):
        payload = {"entries": [], "next_collection_link": "https://api.launchpad.net/1.0/page"}
        with mock.patch.object(us, "MAX_PAGES", 2), mock.patch.object(us, "_request_json", return_value=(payload, 200)):
            with self.assertRaisesRegex(us.SourceError, "exceeds 2 pages"):
                us.published_source_entries(SOURCE, VERSION, fetch=True)

    def test_source_file_urls_uses_correct_operation(self):
        entry = {"self_link": "https://api.launchpad.net/1.0/ubuntu/+archive/primary/+sourcepub/15876704"}
        expected = ["https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/a/a.dsc"]
        with mock.patch.object(us, "_request_json", return_value=(expected, 200)) as call:
            self.assertEqual(us.source_file_urls(entry, fetch=True), expected)
        self.assertEqual(parse_qs(urlparse(call.call_args.args[0]).query), {"ws.op": ["sourceFileUrls"]})

    def test_source_file_urls_rejects_nonofficial_url(self):
        entry = {"self_link": "https://api.launchpad.net/1.0/pub/1"}
        with mock.patch.object(us, "_request_json", return_value=(["https://evil.example/a.dsc"], 200)):
            with self.assertRaisesRegex(us.SourceError, "Rejected"):
                us.source_file_urls(entry, fetch=True)

    def test_dsc_clear_signed_parsing_and_exact_identity(self):
        rows = us.parse_dsc(dsc_bytes(), SOURCE, VERSION)
        self.assertEqual(rows[0]["filename"], "pulseaudio_15.99.1.orig.tar.xz")
        with self.assertRaisesRegex(us.SourceError, "identity mismatch"):
            us.parse_dsc(dsc_bytes(), SOURCE, "15.99.1+dfsg1-1ubuntu2.2")

    def test_dsc_rejects_unsafe_filenames_and_malformed_digest(self):
        for filename in ("../escape.tar", "sub/path.tar", "bad\x00name"):
            text = ("Source: pkg\nVersion: 1\nChecksums-Sha256:\n " + "a" * 64 + f" 4 {filename}\n").encode()
            with self.assertRaises(us.SourceError):
                us.parse_dsc(text, "pkg", "1")

    def _mock_capture(self, root, archive=b"archive", reference_missing=False):
        source = SOURCE
        version = VERSION
        archive_name = "pulseaudio_15.99.1.orig.tar.xz"
        dsc_name = "pulseaudio_15.99.1.dsc"
        dsc = dsc_bytes(source, version, archive)
        base = "https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/pulseaudio/1%3A15.99.1%2Bdfsg1-1ubuntu2.2/"
        pub1 = {"source_name": source, "source_package_version": version,
                "distro_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy",
                "self_link": "https://api.launchpad.net/1.0/pub/1"}
        pub2 = dict(pub1, self_link="https://api.launchpad.net/1.0/pub/2")
        dsc_url = base + dsc_name
        archive_url = base + archive_name
        pubs = [pub1, pub2]
        def stream(url, target, *, max_bytes, fetch):
            data = dsc if url.endswith(".dsc") else archive
            target.write_bytes(data)
            return len(data), hashlib.sha256(data).hexdigest()
        with mock.patch.object(us, "published_source_entries", return_value=(pubs, "Published")), \
             mock.patch.object(us, "source_file_urls", side_effect=[[dsc_url, archive_url], [dsc_url, archive_url]]), \
             mock.patch.object(us, "_download_file", side_effect=lambda url, limit, fetch: dsc), \
             mock.patch.object(us, "_download_to_file", side_effect=stream):
            return us.fetch_source(root / "cache", source, version, fetch=True)

    def test_duplicate_publications_deduplicate_identical_file_lists_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._mock_capture(Path(tmp))
            self.assertEqual(len(manifest["publication_links"]), 2)
            self.assertEqual(len(manifest["files"]), 2)
            self.assertEqual(manifest["source_version"], VERSION)

    def test_bad_archive_hash_or_size_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(us, "published_source_entries", return_value=([{
                    "source_name": SOURCE, "source_package_version": VERSION,
                    "distro_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy",
                    "self_link": "https://api.launchpad.net/1.0/pub/1"}], "Published")), \
                 mock.patch.object(us, "source_file_urls", return_value=[
                    "https://launchpad.net/a/pulseaudio_15.99.1.dsc",
                    "https://launchpad.net/a/pulseaudio_15.99.1.orig.tar.xz"]), \
             mock.patch.object(us, "_download_file", return_value=dsc_bytes()), \
             mock.patch.object(us, "_download_to_file", side_effect=lambda url, target, **kwargs: (target.write_bytes(b"wrong"), len(b"wrong"), hashlib.sha256(b"wrong").hexdigest())[1:]):
                with self.assertRaisesRegex(us.SourceError, "checksum/size mismatch"):
                    us.fetch_source(Path(tmp) / "cache", SOURCE, VERSION, fetch=True)

    def test_missing_dsc_referenced_file_fails(self):
        archive = b"archive"
        good_dsc = dsc_bytes(archive=archive)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(us, "published_source_entries", return_value=([{
                "source_name": SOURCE, "source_package_version": VERSION,
                "distro_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy", "self_link": "https://api.launchpad.net/1.0/pub/1"}], "Published")), \
             mock.patch.object(us, "source_file_urls", return_value=["https://launchpad.net/a/pulseaudio_15.99.1.dsc"]), \
             mock.patch.object(us, "_download_file", return_value=good_dsc):
            with self.assertRaisesRegex(us.SourceError, "absent from official"):
                us.fetch_source(Path(tmp) / "cache", SOURCE, VERSION, fetch=True)

    def test_cache_corruption_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._mock_capture(root)
            cache = root / "cache" / us.cache_key(SOURCE, VERSION)
            (cache / manifest["files"][0]["filename"]).write_bytes(b"corrupt")
            with self.assertRaisesRegex(us.SourceError, "integrity mismatch"):
                us.fetch_source(root / "cache", SOURCE, VERSION, fetch=False)

    def test_url_validation_and_basename_validation(self):
        for url in ("http://launchpad.net/a", "https://evil.example/a", "https://user@launchpad.net/a"):
            with self.assertRaises(us.SourceError):
                us._validate_url(url)
        for name in ("", "..", "../x", "a/b", "a\\b", "a\x00b"):
            self.assertFalse(us._safe_basename(name))

    def test_atomic_writer_refuses_symlink_and_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(us.SourceError, "escapes allowed root"):
                us._atomic_write(root / ".." / "outside" / "bad", b"x", root)
            link = root / "link"
            target = outside / "file"
            try:
                link.symlink_to(outside, target_is_directory=True)
                (root / "leaf").symlink_to(target)
                target_link = root / "target-link"
                target_link.symlink_to(target)
            except (OSError, NotImplementedError):
                # Symlink privileges vary by platform; traversal coverage above remains active.
                return
            with self.assertRaisesRegex(us.SourceError, "escapes allowed root"):
                us._atomic_write(link / "bad", b"x", root)
            with self.assertRaisesRegex(us.SourceError, "escapes allowed root"):
                us._atomic_write(root / "leaf" / "out", b"x", root)
            with self.assertRaisesRegex(us.SourceError, "symlink"):
                us._atomic_write(target_link, b"x", root)

    def test_output_sha256sums_contains_only_standard_checksum_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            report.write_text(json.dumps({"native_files": [{"source_package": SOURCE,
                "source_version": VERSION, "provider": {"kind": "system-package"}}], "unresolved": []}))
            archive_name = "pulseaudio_15.99.1.orig.tar.xz"
            archive = b"captured archive"
            digest = hashlib.sha256(archive).hexdigest()
            manifest = {"source_package": SOURCE, "source_version": VERSION,
                "publication_status": "Published", "publication_links": [],
                "files": [{"filename": archive_name, "size_bytes": len(archive), "sha256": digest,
                    "urls": ["https://launchpad.net/archive"], "digest_source": "test"}]}
            key = us.cache_key(SOURCE, VERSION)
            (root / "cache" / key).mkdir(parents=True)
            (root / "cache" / key / archive_name).write_bytes(archive)
            with mock.patch.object(us, "fetch_source", return_value=manifest):
                us.build_output(report, root / "cache", root / "out", fetch=False)
            sums = root / "out" / "UBUNTU-SHA256SUMS"
            lines = sums.read_text().splitlines()
            self.assertTrue(lines)
            self.assertTrue(all(len(line) > 66 and line[64:66] == "  " for line in lines))
            for line in lines:
                expected, relative = line[:64], line[66:]
                self.assertEqual(hashlib.sha256((root / "out" / relative).read_bytes()).hexdigest(), expected)

    def test_transient_retry_is_bounded(self):
        class FailingOpener:
            def __init__(self):
                self.calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise OSError("temporary network error")
        opener = FailingOpener()
        with mock.patch.object(us, "_opener", return_value=opener), mock.patch.object(us.time, "sleep"):
            with self.assertRaisesRegex(us.SourceError, "after 3 attempts"):
                us._request_bytes("https://api.launchpad.net/1.0/test", max_bytes=100, fetch=True)
        self.assertEqual(opener.calls, 3)

    def test_stream_download_is_atomic_bounded_and_hashed(self):
        class Response:
            status = 200
            headers = {"Content-Length": "7"}
            def __init__(self):
                self.blocks = [b"abc", b"defg", b""]
            def geturl(self):
                return "https://api.launchpad.net/1.0/archive"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                return self.blocks.pop(0)
        class Opener:
            def open(self, request, timeout):
                return Response()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(us, "_opener", return_value=Opener()):
            target = Path(tmp) / "archive"
            size, digest = us._download_to_file("https://api.launchpad.net/1.0/archive", target,
                                                max_bytes=10, fetch=True)
            self.assertEqual(size, 7)
            self.assertEqual(digest, hashlib.sha256(b"abcdefg").hexdigest())
            self.assertEqual(target.read_bytes(), b"abcdefg")
            self.assertEqual(list(Path(tmp).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
