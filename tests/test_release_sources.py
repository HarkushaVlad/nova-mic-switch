from __future__ import annotations

import hashlib
import json
import os
import socket
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_release_sources import (
    MAX_BUNDLE_BYTES,
    ReleaseSourceError,
    _add_file,
    _assert_output_disjoint,
    build_bundle,
    compare_linux_coverage,
    preflight_tar_size,
    safe_relative,
)


COMMIT = "746209b34bdfe5bc81dae2589ce82bc069225bc6"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, value: object) -> bytes:
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    _write(path, raw)
    return raw


def make_fixture(root: Path, *, unresolved: bool = False) -> dict[str, Path]:
    project = root / "project"
    upstream = root / "upstream"
    ubuntu = root / "ubuntu"
    runtime = root / "runtime"
    output = root / "output"
    report_dir = root / "report"
    for path in (project, upstream, ubuntu, runtime, report_dir):
        path.mkdir(parents=True)

    # Fill the explicit project build/source whitelist with safe small fixture bytes.
    from scripts.build_release_sources import PROJECT_FILES

    for rel in PROJECT_FILES:
        if rel == "pyproject.toml":
            data = b'[project]\nname = "nova-mic-switch"\nversion = "0.1.0"\n'
        else:
            data = f"fixture:{rel}\n".encode()
        _write(project / rel, data)
    _write(project / "src/nova_mic_switch/__init__.py", b"# fixture module\n")

    upstream_assets = []
    recipe_body = b"recipe payload\n"
    recipe_name = "qt-6.12.0-ffmpeg-recipes.tar.gz"
    recipe_path = upstream / recipe_name
    with tarfile.open(recipe_path, "w:gz") as archive:
        info = tarfile.TarInfo("recipes-root/coin/recipe.sh")
        info.size = len(recipe_body)
        archive.addfile(info, __import__("io").BytesIO(recipe_body))
    recipe_bytes = recipe_path.read_bytes()
    assets = [
        ("qt-everywhere-src", "qt-everywhere-src-6.12.0.tar.xz", b"qt-source", "6.12.0"),
        ("pyside-setup", "pyside-setup-test.tar.gz", b"pyside-source", "6.12.0"),
        ("qt5-supermodule-source", "qt5-test.tar.gz", b"qt5-source", "6.12.0"),
        ("ffmpeg", "ffmpeg-9.0.1.tar.gz", b"ffmpeg-source", "9.0.1"),
        ("qt5-ffmpeg-recipes", recipe_name, recipe_bytes, "6.12.0"),
    ]
    for asset_id, filename, data, version in assets:
        _write(upstream / filename, data)
        record = {
            "asset_id": asset_id,
            "filename": filename,
            "version": version,
            "sha256": _sha(data),
            "size_bytes": len(data),
            "url": f"https://example.invalid/{filename}",
            "verification_basis": "fixture input",
        }
        if asset_id == "qt5-ffmpeg-recipes":
            record["manifest"] = {
                "files": [{"path": "coin/recipe.sh", "sha256": _sha(recipe_body), "size_bytes": len(recipe_body)}]
            }
        upstream_assets.append(record)

    pinned_assets = []
    for asset_id, filename, data, version in assets[:-1]:
        item = {"id": asset_id, "filename": filename, "version": version, "url": f"https://example.invalid/{filename}", "sha256": _sha(data), "max_bytes": 100000}
        if asset_id == "ffmpeg":
            item["sha1"] = hashlib.sha1(data).hexdigest()
        pinned_assets.append(item)
    _write_json(project / "packaging/sources/upstream.json", {
        "schema_version": 1,
        "assets": pinned_assets,
        "qt_ffmpeg_recipes": {
            "repository": "qt/qt5",
            "tag": "v6.12.0",
            "resolved_commit": "a" * 40,
            "archive_filename": recipe_name,
            "sha256": _sha(recipe_bytes),
        },
    })
    _write_json(upstream / "source-index.json", {"schema_version": 1, "assets": upstream_assets})
    _write(upstream / "SOURCE-INDEX.md", b"# fixture upstream sources\n")
    sums = "".join(f"{record['sha256']}  {record['filename']}\n" for record in sorted(upstream_assets, key=lambda x: x["filename"]))
    _write(upstream / "SHA256SUMS", sums.encode())

    ubuntu_data = b"ubuntu-source-data\n"
    ubuntu_rel = "sources/" + "1" * 64 + "/libfixture_1.0.orig.tar.xz"
    _write(ubuntu / ubuntu_rel, ubuntu_data)
    pair = {"source_package": "libfixture", "source_version": "1:1.0-2"}
    ubuntu_record = {
        **pair,
        "cache_key": "1" * 64,
        "files": [{"path": ubuntu_rel, "filename": "libfixture_1.0.orig.tar.xz", "size_bytes": len(ubuntu_data), "sha256": _sha(ubuntu_data), "urls": ["https://example.invalid/libfixture"]}],
    }
    _write_json(ubuntu / "ubuntu-source-index.json", {"schema_version": 1, "sources": [ubuntu_record], "coverage": {"unresolved_native_count": int(unresolved)}})
    _write(ubuntu / "UBUNTU-SHA256SUMS", f"{_sha(ubuntu_data)}  {ubuntu_rel}\n".encode())

    runtime_records = []
    runtime_assets = [
        ("runtime-a.tar.gz", b"runtime a"),
        ("runtime-b.tar.xz", b"runtime b"),
        ("runtime-c.tar.gz", b"runtime c"),
    ]
    for filename, data in runtime_assets:
        rel = f"sources/{filename}"
        _write(runtime / rel, data)
        runtime_records.append({
            "filename": filename,
            "url": f"https://example.invalid/{filename}",
            "archive_path": f"build/runtime-materials/{rel}",
            "archive_bytes": len(data),
            "archive_sha256": _sha(data),
            "version": "fixture",
        })

    def notice(rel: str, data: bytes) -> dict[str, object]:
        _write(project / rel, data)
        return {"destination": rel, "sha256": _sha(data), "bytes": len(data), "source_url": "https://example.invalid/notice"}

    qt_notice = notice("packaging/licenses/qt-native/LICENSE.txt", b"qt notice\n")
    qt6_notice = notice("packaging/licenses/qt-6.12.0/LICENSE.txt", b"qt6 notice\n")
    ff_notice = notice("packaging/licenses/ffmpeg-9.0.1/LICENSE.txt", b"ffmpeg notice\n")
    runtime_notice = notice("packaging/licenses/appimage-runtime/LICENSE.txt", b"runtime notice\n")
    win_notice = notice("packaging/licenses/windows-native/LICENSE.txt", b"windows notice\n")
    permissive_rel = "packaging/licenses/appimage-runtime/permissive/zlib/LICENSE"
    permissive_notice = notice(permissive_rel, b"zlib notice\n")

    _write_json(project / "packaging/licenses/qt-native/manifest.json", {"schema_version": 1, "copied_text_files": [qt_notice]})
    _write_json(project / "packaging/licenses/qt-6.12.0/manifest.json", {"schema_version": 1, "licenses": [{"filename": "LICENSE.txt", "sha256": qt6_notice["sha256"], "bytes": qt6_notice["bytes"]}]})
    _write_json(project / "packaging/licenses/ffmpeg-9.0.1/manifest.json", {"schema_version": 1, "licenses": [{"filename": "LICENSE.txt", "sha256": ff_notice["sha256"], "bytes": ff_notice["bytes"]}]})
    _write_json(project / "packaging/licenses/appimage-runtime/manifest.json", {
        "schema_version": 1,
        "sources": runtime_records,
        "copied_texts": [runtime_notice],
        "additional_materials": [],
    })
    _write_json(project / "packaging/licenses/appimage-runtime/permissive/manifest.json", {
        "materials": [{"path": "zlib/LICENSE", "sha256": permissive_notice["sha256"], "size_bytes": permissive_notice["bytes"]}]
    })
    _write_json(project / "packaging/licenses/windows-native/manifest.json", {"schema_version": 1, "copied_text_files": [win_notice]})

    report_rows = [{"provider": {"kind": "system-package"}, "source_package": pair["source_package"], "source_version": pair["source_version"]}]
    if unresolved:
        report_rows.append({"provider": {"kind": "unresolved"}, "source_package": None, "source_version": None})
    _write_json(report_dir / "report.json", {"native_files": report_rows})
    return {
        "project": project,
        "upstream": upstream,
        "ubuntu": ubuntu,
        "runtime": runtime,
        "output": output,
        "report": report_dir / "report.json",
    }


class ReleaseSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = make_fixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, **overrides: object) -> dict[str, object]:
        args = {
            "upstream_dir": self.fixture["upstream"],
            "ubuntu_dir": self.fixture["ubuntu"],
            "runtime_dir": self.fixture["runtime"],
            "output_dir": self.fixture["output"],
            "linux_report": self.fixture["report"],
            "application_commit": COMMIT,
            "repo_root": self.fixture["project"],
        }
        args.update(overrides)
        return build_bundle(**args)  # type: ignore[arg-type]

    def test_build_is_deterministic_and_tar_metadata_is_fixed(self) -> None:
        with patch.object(urllib.request, "urlopen", side_effect=AssertionError("network access")), patch.object(
            socket, "create_connection", side_effect=AssertionError("network access")
        ):
            first = self.build()
            archive_path = Path(str(first["archive"]))
            first_bytes_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            second = self.build()
        self.assertEqual(first_bytes_hash, second["archive_sha256"])
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        bundle_index = json.loads((self.fixture["output"] / "source-bundle-index.json").read_text())
        self.assertEqual(bundle_index["estimated_tar_bytes"], Path(str(first["archive"])).stat().st_size)
        self.assertFalse(first["release_ready"])
        self.assertTrue(first["linux_source_coverage"]["linux_source_coverage_complete"])
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            self.assertEqual(names, sorted(names))
            self.assertIn("upstream/qt-everywhere-src-6.12.0.tar.xz", names)
            self.assertIn("ubuntu/sources/" + "1" * 64 + "/libfixture_1.0.orig.tar.xz", names)
            self.assertIn("source-bundle-index.json", names)
            self.assertTrue(all(m.mtime == 0 and m.uid == 0 and m.gid == 0 and m.uname == "" and m.gname == "" for m in members))
            self.assertTrue(all(m.isfile() for m in members))
            by_name = {member.name: member for member in members}
            self.assertIn("project/scripts/collect_linux_notices.py", by_name)
            self.assertEqual(by_name["project/packaging/linux/AppRun"].mode, 0o755)
            self.assertEqual(by_name["project/scripts/build_appimage.sh"].mode, 0o755)
            self.assertEqual(by_name["project/scripts/build_linux.sh"].mode, 0o755)
            self.assertEqual(by_name["project/scripts/collect_linux_notices.py"].mode, 0o755)
            self.assertEqual(by_name["project/scripts/build_release_sources.py"].mode, 0o755)
            self.assertEqual(by_name["project/packaging/linux/nova-mic-switch.desktop"].mode, 0o644)
            self.assertEqual(by_name["project/packaging/icons/nova-mic-switch.ico"].mode, 0o644)
            for copied, curated in (
                ("notices/qt-native/LICENSE.txt", "project/packaging/licenses/qt-native/LICENSE.txt"),
                ("notices/manifests/qt-native.json", "project/packaging/licenses/qt-native/manifest.json"),
                ("notices/windows-native/LICENSE.txt", "project/packaging/licenses/windows-native/LICENSE.txt"),
            ):
                self.assertEqual(archive.extractfile(copied).read(), archive.extractfile(curated).read())
        checksum = Path(str(first["archive"]) + ".sha256").read_text(encoding="ascii")
        self.assertEqual(checksum, f"{first['archive_sha256']}  {archive_path.name}\n")

    def test_unknown_provider_blocks_require_complete_but_is_reported(self) -> None:
        fixture = make_fixture(self.root / "unknown", unresolved=True)
        result = build_bundle(
            upstream_dir=fixture["upstream"], ubuntu_dir=fixture["ubuntu"], runtime_dir=fixture["runtime"],
            output_dir=fixture["output"], linux_report=fixture["report"], application_commit=COMMIT,
            repo_root=fixture["project"],
        )
        self.assertFalse(result["release_ready"])
        self.assertEqual(result["linux_source_coverage"]["unknown_native_provider_rows"], 1)
        with self.assertRaisesRegex(ReleaseSourceError, "unknown native providers=1"):
            build_bundle(
                upstream_dir=fixture["upstream"], ubuntu_dir=fixture["ubuntu"], runtime_dir=fixture["runtime"],
                output_dir=self.root / "strict-output", linux_report=fixture["report"], application_commit=COMMIT,
                require_complete=True, repo_root=fixture["project"],
            )

    def test_exact_epoch_version_pair_is_required(self) -> None:
        report = {"native_files": [{"provider": {"kind": "system-package"}, "source_package": "libfixture", "source_version": "1.0-2"}]}
        index = {"sources": [{"source_package": "libfixture", "source_version": "1:1.0-2"}]}
        result = compare_linux_coverage(report, index)
        self.assertEqual(result["missing_exact_pairs_from_ubuntu_index"], [["libfixture", "1.0-2"]])
        self.assertEqual(result["ubuntu_index_pairs_not_in_report"], [["libfixture", "1:1.0-2"]])
        self.assertFalse(result["linux_source_coverage_complete"])

    def test_extra_captured_ubuntu_pairs_do_not_block_coverage(self) -> None:
        report = {"native_files": [{"provider": {"kind": "system-package"}, "source_package": "libfixture", "source_version": "1:1.0-2"}]}
        index = {"sources": [
            {"source_package": "libfixture", "source_version": "1:1.0-2"},
            {"source_package": "old-extra", "source_version": "2.0-1"},
        ]}
        result = compare_linux_coverage(report, index)
        self.assertTrue(result["linux_source_coverage_complete"])
        self.assertEqual(result["ubuntu_index_pairs_not_in_report"], [["old-extra", "2.0-1"]])

    def test_require_complete_accepts_report_subset_of_verified_index(self) -> None:
        index_path = self.fixture["ubuntu"] / "ubuntu-source-index.json"
        index = json.loads(index_path.read_text())
        data = b"previously captured source archive\n"
        rel = "sources/" + "2" * 64 + "/old-extra_2.0.orig.tar.xz"
        _write(self.fixture["ubuntu"] / rel, data)
        index["sources"].append({
            "source_package": "old-extra",
            "source_version": "2.0-1",
            "cache_key": "2" * 64,
            "files": [{"path": rel, "filename": "old-extra_2.0.orig.tar.xz", "size_bytes": len(data), "sha256": _sha(data), "urls": ["https://example.invalid/old-extra"]}],
        })
        _write_json(index_path, index)
        sums = {
            record["path"]: record["sha256"]
            for source in index["sources"]
            for record in source["files"]
        }
        _write(self.fixture["ubuntu"] / "UBUNTU-SHA256SUMS", "".join(f"{digest}  {path}\n" for path, digest in sorted(sums.items())).encode())
        result = self.build(require_complete=True)
        self.assertTrue(result["linux_source_coverage"]["linux_source_coverage_complete"])
        self.assertFalse(result["release_ready"])

    def test_require_complete_rejects_missing_exact_epoch_pair(self) -> None:
        report_path = self.fixture["report"]
        report = json.loads(report_path.read_text())
        report["native_files"][0]["source_version"] = "1.0-2"
        _write_json(report_path, report)
        with self.assertRaisesRegex(ReleaseSourceError, "missing pairs"):
            self.build(require_complete=True)

    def test_missing_and_unrecognized_provider_kinds_count_as_unknown(self) -> None:
        report = {"native_files": [
            {"source_package": "ignored", "source_version": "1"},
            {"provider": {"kind": "future-provider"}, "source_package": "ignored", "source_version": "1"},
        ]}
        result = compare_linux_coverage(report, {"sources": []})
        self.assertEqual(result["unknown_native_provider_rows"], 2)
        self.assertEqual(result["unknown_native_provider_kinds"], {"future-provider": 1, "unknown-provider-kind": 1})
        self.assertFalse(result["linux_source_coverage_complete"])

    def test_input_hash_mismatch_fails_without_repinning(self) -> None:
        path = self.fixture["upstream"] / "ffmpeg-9.0.1.tar.gz"
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ReleaseSourceError, "SHA-256 mismatch"):
            self.build()

    def test_traversal_in_ubuntu_index_fails(self) -> None:
        index_path = self.fixture["ubuntu"] / "ubuntu-source-index.json"
        data = json.loads(index_path.read_text())
        data["sources"][0]["files"][0]["path"] = "../escape.tar.gz"
        _write_json(index_path, data)
        with self.assertRaisesRegex(ReleaseSourceError, "Unsafe path component"):
            self.build()

    def test_duplicate_notice_destinations_fail(self) -> None:
        path = self.fixture["project"] / "packaging/licenses/qt-native/manifest.json"
        data = json.loads(path.read_text())
        data["copied_text_files"].append(dict(data["copied_text_files"][0]))
        _write_json(path, data)
        with self.assertRaisesRegex(ReleaseSourceError, "Duplicate or case-colliding"):
            self.build()

    def test_symlink_input_file_is_rejected(self) -> None:
        source = self.fixture["upstream"] / "ffmpeg-9.0.1.tar.gz"
        target = self.root / "external-source"
        target.write_bytes(source.read_bytes())
        source.unlink()
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ReleaseSourceError, "regular non-symlink"):
            self.build()

    def test_output_directory_symlink_is_rejected(self) -> None:
        output_target = self.root / "output-target"
        output_target.mkdir()
        output_link = self.root / "output-link"
        try:
            output_link.symlink_to(output_target, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with patch("scripts.build_release_sources._canonicalize_for_overlap", side_effect=AssertionError("canonicalized symlink")):
            with self.assertRaisesRegex(ReleaseSourceError, "Unsafe output directory parent"):
                self.build(output_dir=output_link)

    def test_input_root_symlink_is_rejected_before_overlap_canonicalization(self) -> None:
        alias = self.root / "upstream-alias"
        try:
            alias.symlink_to(self.fixture["upstream"], target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with patch("scripts.build_release_sources._canonicalize_for_overlap", side_effect=AssertionError("canonicalized symlink")):
            with self.assertRaisesRegex(ReleaseSourceError, "Symlink in upstream source directory path"):
                self.build(upstream_dir=alias)

    def test_output_equal_to_or_nested_under_an_input_fails_before_creation(self) -> None:
        upstream = self.fixture["upstream"]
        with self.assertRaisesRegex(ReleaseSourceError, "overlaps a source input"):
            self.build(output_dir=upstream)
        nested = upstream / "generated-output"
        with self.assertRaisesRegex(ReleaseSourceError, "overlaps a source input"):
            self.build(output_dir=nested)
        self.assertFalse(nested.exists())

    def test_short_and_long_path_aliases_overlap_after_canonicalization(self) -> None:
        output_alias = Path("C:/Users/RUNNER~1/source-data/release")
        input_alias = Path("C:/Users/runneradmin/source-data")
        aliases = {
            str(output_alias): Path("C:/Users/runneradmin/source-data/release"),
            str(input_alias): Path("C:/Users/runneradmin/source-data"),
        }
        with patch(
            "scripts.build_release_sources._canonicalize_for_overlap",
            side_effect=lambda path: aliases[str(path)],
        ) as canonicalize:
            with self.assertRaisesRegex(ReleaseSourceError, "overlaps a source input"):
                _assert_output_disjoint(output_alias, (input_alias,))
        self.assertEqual(canonicalize.call_count, 2)

    def test_tar_path_and_size_limits(self) -> None:
        with self.assertRaises(ReleaseSourceError):
            safe_relative("a/../b")
        with self.assertRaises(ReleaseSourceError):
            safe_relative("C:/unsafe")
        with self.assertRaisesRegex(ReleaseSourceError, "exceeds"):
            preflight_tar_size([("payload.bin", MAX_BUNDLE_BYTES)])

    def test_binary_artifact_types_are_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseSourceError, "not allowed in the source bundle"):
            _add_file(
                [], set(), self.fixture["upstream"], "ffmpeg-9.0.1.tar.gz", "project/native.dll",
                "test", "test fixture",
            )

    def test_application_commit_format_is_required(self) -> None:
        with self.assertRaisesRegex(ReleaseSourceError, "40 hexadecimal"):
            self.build(application_commit="abc")


if __name__ == "__main__":
    unittest.main()
