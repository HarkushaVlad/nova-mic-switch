#!/usr/bin/env python3
"""Build a deterministic, offline corresponding-source bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable


MAX_BUNDLE_BYTES = 1_900_000_000
HASH_CHUNK_SIZE = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]

PROJECT_FILES = (
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "docs/LIBRARY-REPLACEMENT.md",
    "packaging/frozen_entry.py",
    "packaging/nova-mic-switch.spec",
    "packaging/sources/upstream.json",
    "packaging/release-tools.json",
    "packaging/linux/AppRun",
    "packaging/linux/nova-mic-switch.desktop",
    "packaging/icons/nova-mic-switch.svg",
    "packaging/icons/nova-mic-switch.png",
    "packaging/icons/nova-mic-switch.ico",
    "packaging/windows/installer.iss",
    "packaging/windows/native_policy.py",
    "scripts/build_appimage.sh",
    "scripts/build_linux.sh",
    "scripts/build_windows.ps1",
    "scripts/build_source_assets.py",
    "scripts/build_ubuntu_sources.py",
    "scripts/collect_licenses.py",
    "scripts/collect_linux_notices.py",
    "scripts/native_report.py",
    "scripts/release_tools.py",
    "scripts/build_release_sources.py",
)

EXECUTABLE_PROJECT_FILES = frozenset(
    {
        "packaging/linux/AppRun",
        "scripts/build_appimage.sh",
        "scripts/build_linux.sh",
        "scripts/build_source_assets.py",
        "scripts/build_ubuntu_sources.py",
        "scripts/collect_licenses.py",
        "scripts/collect_linux_notices.py",
        "scripts/native_report.py",
        "scripts/release_tools.py",
        "scripts/build_release_sources.py",
    }
)

NOTICE_GROUPS = (
    ("qt-native", "packaging/licenses/qt-native/manifest.json", "copied_text_files"),
    ("qt-6.12.0", "packaging/licenses/qt-6.12.0/manifest.json", "licenses"),
    ("ffmpeg-9.0.1", "packaging/licenses/ffmpeg-9.0.1/manifest.json", "licenses"),
    ("appimage-runtime", "packaging/licenses/appimage-runtime/manifest.json", "copied_texts"),
    (
        "appimage-runtime-permissive",
        "packaging/licenses/appimage-runtime/permissive/manifest.json",
        "materials",
    ),
    ("windows-native", "packaging/licenses/windows-native/manifest.json", "copied_text_files"),
)


class ReleaseSourceError(ValueError):
    """Raised when a source input is unsafe, incomplete, or fails verification."""


@dataclass(frozen=True)
class InputFile:
    root: Path
    relative_path: str
    bundle_path: str
    kind: str
    source: str
    sha256: str
    size_bytes: int
    mode: int

    def record(self) -> dict[str, Any]:
        return {
            "path": self.bundle_path,
            "kind": self.kind,
            "source": self.source,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mode": f"{self.mode:04o}",
        }


def safe_relative(value: str, label: str = "path") -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ReleaseSourceError(f"Unsafe {label}: {value!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ReleaseSourceError(f"Control character in {label}: {value!r}")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) or ":" in value:
        raise ReleaseSourceError(f"Absolute or drive-qualified {label}: {value!r}")
    pieces = value.split("/")
    if any(piece in ("", ".", "..") for piece in pieces):
        raise ReleaseSourceError(f"Unsafe path component in {label}: {value!r}")
    if len(value.encode("utf-8")) > 255 or any(len(piece.encode("utf-8")) > 100 for piece in pieces):
        raise ReleaseSourceError(f"Path exceeds deterministic USTAR limits: {value!r}")
    return value


def _root_dir(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    for component in list(reversed(absolute.parents)) + [absolute]:
        try:
            info = component.lstat()
        except OSError as exc:
            raise ReleaseSourceError(f"Missing {label}: {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseSourceError(f"Symlink in {label} path: {component}")
        if component != absolute and not stat.S_ISDIR(info.st_mode):
            raise ReleaseSourceError(f"Non-directory parent in {label} path: {component}")
    if not stat.S_ISDIR(info.st_mode):
        raise ReleaseSourceError(f"{label} must be a directory: {path}")
    return absolute.resolve(strict=True)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _open_regular(root: Path, rel: str, label: str) -> tuple[BinaryIO, os.stat_result]:
    rel = safe_relative(rel, label)
    base = _root_dir(root, label)
    current = base
    parts = rel.split("/")
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ReleaseSourceError(f"Missing {label}: {rel}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseSourceError(f"Symlink or non-directory in {label}: {rel}")
    target = current / parts[-1]
    try:
        before = target.lstat()
    except OSError as exc:
        raise ReleaseSourceError(f"Missing {label}: {rel}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseSourceError(f"{label} must be a regular non-symlink file: {rel}")
    try:
        target.resolve(strict=True).relative_to(base)
    except (OSError, ValueError) as exc:
        raise ReleaseSourceError(f"{label} escapes its input root: {rel}") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise ReleaseSourceError(f"Unable to safely open {label}: {rel}") from exc
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise ReleaseSourceError(f"{label} changed while opening: {rel}")
    return os.fdopen(fd, "rb"), opened


def _hash_file(root: Path, rel: str, label: str) -> tuple[str, int]:
    stream, info = _open_regular(root, rel, label)
    digest = hashlib.sha256()
    size = 0
    with stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    if size != info.st_size:
        raise ReleaseSourceError(f"{label} changed while hashing: {rel}")
    return digest.hexdigest(), size


def _read_small_json(root: Path, rel: str, label: str, limit: int = 16 * 1024 * 1024) -> tuple[dict[str, Any], bytes]:
    stream, info = _open_regular(root, rel, label)
    if info.st_size > limit:
        stream.close()
        raise ReleaseSourceError(f"{label} exceeds JSON size limit: {rel}")
    with stream:
        raw = stream.read(limit + 1)
    if len(raw) != info.st_size or len(raw) > limit:
        raise ReleaseSourceError(f"{label} changed or exceeded its limit: {rel}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseSourceError(f"Invalid UTF-8 JSON in {label}: {rel}") from exc
    if not isinstance(value, dict):
        raise ReleaseSourceError(f"Expected JSON object in {label}: {rel}")
    return value, raw


def _read_small_text(root: Path, rel: str, label: str, limit: int = 16 * 1024 * 1024) -> bytes:
    stream, info = _open_regular(root, rel, label)
    if info.st_size > limit:
        stream.close()
        raise ReleaseSourceError(f"{label} exceeds size limit: {rel}")
    with stream:
        raw = stream.read(limit + 1)
    if len(raw) != info.st_size or len(raw) > limit or not raw:
        raise ReleaseSourceError(f"Empty, changed, or oversized {label}: {rel}")
    if b"\x00" in raw:
        raise ReleaseSourceError(f"NUL byte in text input {label}: {rel}")
    return raw


def _add_file(
    files: list[InputFile],
    paths: set[str],
    root: Path,
    relative_path: str,
    bundle_path: str,
    kind: str,
    source: str,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    text: bool = False,
) -> InputFile:
    relative_path = safe_relative(relative_path, "input path")
    bundle_path = safe_relative(bundle_path, "bundle path")
    if Path(bundle_path).suffix.casefold() in {".dll", ".whl", ".7z", ".exe"}:
        raise ReleaseSourceError(f"Executable/native binary artifact is not allowed in the source bundle: {bundle_path}")
    key = bundle_path.casefold()
    if key in paths:
        raise ReleaseSourceError(f"Duplicate or case-colliding bundle path: {bundle_path}")
    if text:
        data = _read_small_text(root, relative_path, source)
        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)
    else:
        sha256, size = _hash_file(root, relative_path, source)
    if expected_sha256 is not None and sha256.lower() != expected_sha256.lower():
        raise ReleaseSourceError(f"SHA-256 mismatch for {source} ({relative_path}): {sha256}")
    if expected_size is not None and size != expected_size:
        raise ReleaseSourceError(f"Size mismatch for {source} ({relative_path}): {size} != {expected_size}")
    paths.add(key)
    project_relative = bundle_path.removeprefix("project/") if bundle_path.startswith("project/") else None
    mode = 0o755 if project_relative in EXECUTABLE_PROJECT_FILES else 0o644
    item = InputFile(root, relative_path, bundle_path, kind, source, sha256, size, mode)
    files.append(item)
    return item


def _sha256_sums(raw: bytes, label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_no, line in enumerate(raw.decode("ascii", "strict").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if not match:
            raise ReleaseSourceError(f"Malformed SHA256SUMS line {line_no} in {label}")
        digest, rel = match.groups()
        rel = safe_relative(rel, f"{label} member")
        if rel in result:
            raise ReleaseSourceError(f"Duplicate checksum path in {label}: {rel}")
        result[rel] = digest.lower()
    return result


def compare_linux_coverage(report: dict[str, Any], ubuntu_index: dict[str, Any]) -> dict[str, Any]:
    rows = report.get("native_files")
    if not isinstance(rows, list):
        raise ReleaseSourceError("Linux report has no native_files list")
    indexed: set[tuple[str, str]] = set()
    for record in ubuntu_index.get("sources", []):
        if not isinstance(record, dict):
            raise ReleaseSourceError("Invalid Ubuntu source record")
        package = record.get("source_package")
        version = record.get("source_version")
        if not isinstance(package, str) or not package or not isinstance(version, str) or not version:
            raise ReleaseSourceError("Ubuntu source record lacks exact source_package/source_version")
        pair = (package, version)
        if pair in indexed:
            raise ReleaseSourceError(f"Duplicate Ubuntu source pair: {pair!r}")
        indexed.add(pair)

    report_pairs: set[tuple[str, str]] = set()
    unknown: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReleaseSourceError("Invalid Linux native_files row")
        provider = row.get("provider")
        raw_kind = provider.get("kind") if isinstance(provider, dict) else None
        kind = raw_kind if isinstance(raw_kind, str) and raw_kind else "unknown-provider-kind"
        if kind == "system-package":
            package, version = row.get("source_package"), row.get("source_version")
            if not isinstance(package, str) or not package or not isinstance(version, str) or not version:
                unknown[kind] = unknown.get(kind, 0) + 1
            else:
                report_pairs.add((package, version))
        elif kind not in {"python-wheel", "python-runtime", "project-executable"}:
            if len(kind) > 100 or any(ord(char) < 32 or ord(char) == 127 for char in kind):
                kind = "unrecognized-provider-kind"
            unknown[kind] = unknown.get(kind, 0) + 1

    missing = sorted(report_pairs - indexed)
    unused = sorted(indexed - report_pairs)
    unknown_count = sum(unknown.values())
    return {
        "report_system_source_pairs": len(report_pairs),
        "ubuntu_index_source_pairs": len(indexed),
        "missing_exact_pairs_from_ubuntu_index": [list(pair) for pair in missing],
        "ubuntu_index_pairs_not_in_report": [list(pair) for pair in unused],
        "unknown_native_provider_rows": unknown_count,
        "unknown_native_provider_kinds": dict(sorted(unknown.items())),
        "linux_indexed_source_pairs_complete": not missing,
        "linux_source_coverage_complete": not missing and unknown_count == 0,
    }


def _validate_recipe_archive(root: Path, rel: str, files: list[dict[str, Any]], label: str) -> None:
    # Only the tiny, indexed Qt recipe archive is read as a container. Other upstream
    # source archives are preserved byte-for-byte and are never extracted or executed.
    import tarfile

    stream, _ = _open_regular(root, rel, label)
    expected: dict[str, dict[str, Any]] = {}
    for record in files:
        member = safe_relative(record.get("path"), f"{label} recipe member")
        if member in expected:
            raise ReleaseSourceError(f"Duplicate Qt recipe member: {member}")
        expected[member] = record
    found: set[str] = set()
    try:
        with stream, tarfile.open(fileobj=stream, mode="r:gz") as archive:
            for member in archive:
                name = safe_relative(member.name, f"{label} archive member")
                if not member.isfile() or member.issym() or member.islnk():
                    raise ReleaseSourceError(f"Unsafe Qt recipe archive member: {name}")
                matches = [path for path in expected if name == path or name.endswith("/" + path)]
                if len(matches) > 1:
                    raise ReleaseSourceError(f"Ambiguous Qt recipe member path: {name}")
                if not matches:
                    continue
                recipe_path = matches[0]
                if recipe_path in found:
                    raise ReleaseSourceError(f"Duplicate Qt recipe archive member: {recipe_path}")
                exp = expected[recipe_path]
                if member.size != exp.get("size_bytes"):
                    raise ReleaseSourceError(f"Qt recipe member size mismatch: {recipe_path}")
                source = archive.extractfile(member)
                if source is None:
                    raise ReleaseSourceError(f"Unreadable Qt recipe member: {recipe_path}")
                digest = hashlib.sha256()
                count = 0
                with source:
                    while chunk := source.read(HASH_CHUNK_SIZE):
                        count += len(chunk)
                        digest.update(chunk)
                if count != member.size or digest.hexdigest() != exp.get("sha256"):
                    raise ReleaseSourceError(f"Qt recipe member hash mismatch: {recipe_path}")
                found.add(recipe_path)
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseSourceError(f"Unreadable Qt recipe archive: {rel}") from exc
    if found != set(expected):
        raise ReleaseSourceError(f"Missing Qt recipe members: {sorted(set(expected) - found)}")


def _load_project_version(repo_root: Path) -> str:
    data = _read_small_text(repo_root, "pyproject.toml", "project metadata")
    text = data.decode("utf-8")
    section = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", text)
    if not section:
        raise ReleaseSourceError("pyproject.toml has no [project] section")
    match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?$', section.group(1))
    if not match or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]*", match.group(1)):
        raise ReleaseSourceError("pyproject.toml has no safe static project.version")
    return match.group(1)


def _collect_notices(repo_root: Path, files: list[InputFile], paths: set[str]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for group, manifest_path, collection_key in NOTICE_GROUPS:
        manifest, raw = _read_small_json(repo_root, manifest_path, f"{group} notice manifest")
        rel_manifest = safe_relative(manifest_path, "notice manifest")
        _add_file(
            files,
            paths,
            repo_root,
            rel_manifest,
            f"notices/manifests/{group}.json",
            "notice_manifest",
            f"{group} notice index",
            text=True,
        )
        _add_file(
            files,
            paths,
            repo_root,
            rel_manifest,
            f"project/{rel_manifest}",
            "project_curated_notice_manifest",
            f"{group} notice index",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            expected_size=len(raw),
            text=True,
        )
        records = manifest.get(collection_key)
        if not isinstance(records, list):
            raise ReleaseSourceError(f"{group} notice manifest lacks {collection_key}")
        group_count = 0
        for record in records:
            if not isinstance(record, dict):
                raise ReleaseSourceError(f"Invalid {group} notice record")
            digest = record.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ReleaseSourceError(f"Missing/invalid notice SHA-256 in {group}")
            size = record.get("bytes", record.get("size_bytes"))
            if size is not None and (not isinstance(size, int) or size < 0):
                raise ReleaseSourceError(f"Invalid notice size in {group}")
            if collection_key == "licenses":
                name = safe_relative(record.get("filename"), f"{group} notice filename")
                relative = f"packaging/licenses/{group}/{name}"
                bundle = f"notices/{group}/{name}"
            elif collection_key == "materials":
                name = safe_relative(record.get("path"), f"{group} notice path")
                relative = f"packaging/licenses/appimage-runtime/permissive/{name}"
                bundle = f"notices/appimage-runtime/permissive/{name}"
            else:
                destination = record.get("destination")
                prefix = "packaging/licenses/"
                if not isinstance(destination, str) or not destination.startswith(prefix):
                    raise ReleaseSourceError(f"Invalid destination in {group} notice record")
                relative = safe_relative(destination, f"{group} notice destination")
                tail = relative[len(prefix) :]
                if not tail.startswith(group + "/") and group == "windows-native":
                    raise ReleaseSourceError(f"Unexpected Windows notice path: {tail}")
                if group == "qt-native":
                    expected_prefix = "qt-native/"
                elif group == "qt-6.12.0":
                    expected_prefix = "qt-6.12.0/"
                elif group == "ffmpeg-9.0.1":
                    expected_prefix = "ffmpeg-9.0.1/"
                elif group == "appimage-runtime":
                    expected_prefix = "appimage-runtime/"
                else:
                    expected_prefix = "windows-native/"
                if not tail.startswith(expected_prefix):
                    raise ReleaseSourceError(f"Notice path does not match group {group}: {tail}")
                bundle = f"notices/{tail}"
            source_url = record.get("source_url") or record.get("original_member_or_source_url") or record.get("original_member_path") or record.get("archive_member_path") or record.get("path") or group
            _add_file(
                files,
                paths,
                repo_root,
                relative,
                bundle,
                "notice_text",
                str(source_url),
                expected_sha256=digest,
                expected_size=size,
                text=True,
            )
            _add_file(
                files,
                paths,
                repo_root,
                relative,
                f"project/{relative}",
                "project_curated_notice",
                str(source_url),
                expected_sha256=digest,
                expected_size=size,
                text=True,
            )
            group_count += 1
        summary.append({"group": group, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "text_material_count": group_count})

        if group == "appimage-runtime":
            additional = manifest.get("additional_materials", [])
            for record in additional:
                rel = record.get("path")
                name = record.get("filename")
                if not isinstance(rel, str) or not isinstance(name, str):
                    raise ReleaseSourceError("Invalid AppImage additional material")
                rel = safe_relative(rel, "AppImage additional material")
                if not rel.startswith("build/runtime-materials/sources/"):
                    raise ReleaseSourceError(f"Unexpected AppImage additional material path: {rel}")
                _add_file(
                    files,
                    paths,
                    repo_root,
                    rel,
                    f"runtime/texts/{safe_relative(name, 'AppImage material filename')}",
                    "runtime_notice",
                    record.get("url", "AppImage runtime manifest"),
                    expected_sha256=record.get("sha256"),
                    expected_size=record.get("bytes"),
                    text=True,
                )
    return summary


def _collect_project_files(repo_root: Path, files: list[InputFile], paths: set[str]) -> None:
    for rel in PROJECT_FILES:
        _add_file(files, paths, repo_root, rel, f"project/{rel}", "project_build_source", rel)
    source_dir = repo_root / "src" / "nova_mic_switch"
    _root_dir(source_dir, "application Python source directory")
    py_files = sorted(source_dir.glob("*.py"), key=lambda p: p.name)
    if not py_files:
        raise ReleaseSourceError("No application Python source files found")
    for path in py_files:
        rel = f"src/nova_mic_switch/{path.name}"
        _add_file(files, paths, repo_root, rel, f"project/{rel}", "project_application_source", rel)


def _tar_info(path: str, size: int, mode: int = 0o644) -> tarfile.TarInfo:
    path = safe_relative(path, "tar member")
    info = tarfile.TarInfo(path)
    info.size = size
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    info.linkname = ""
    info.devmajor = 0
    info.devminor = 0
    return info


def estimate_tar_size(entries: Iterable[tuple[str, int]]) -> int:
    total = 1024
    for path, size in entries:
        safe_relative(path, "tar member")
        if not isinstance(size, int) or size < 0:
            raise ReleaseSourceError(f"Invalid tar member size: {path}")
        total += 512 + ((size + 511) // 512) * 512
    return ((total + 10239) // 10240) * 10240


def preflight_tar_size(entries: Iterable[tuple[str, int]]) -> int:
    estimated = estimate_tar_size(entries)
    if estimated >= MAX_BUNDLE_BYTES:
        raise ReleaseSourceError(f"Preflight tar size {estimated} exceeds the {MAX_BUNDLE_BYTES} byte limit")
    return estimated


class _VerifyingReader:
    def __init__(self, stream: BinaryIO, expected_size: int):
        self.stream = stream
        self.expected_size = expected_size
        self.size = 0
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.size += len(data)
        self.digest.update(data)
        return data


def _write_tar(temporary_path: Path, files: list[InputFile], index_bytes: bytes, markdown: bytes) -> None:
    docs = {
        "SOURCE-INDEX.md": markdown,
        "source-bundle-index.json": index_bytes,
    }
    with temporary_path.open("xb") as raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        members: list[tuple[str, InputFile | None, bytes | None]] = [
            (item.bundle_path, item, None) for item in files
        ]
        members.extend((name, None, data) for name, data in docs.items())
        members.sort(key=lambda row: row[0])
        for name, item, raw_bytes in members:
            if item is None:
                assert raw_bytes is not None
                archive.addfile(_tar_info(name, len(raw_bytes)), io.BytesIO(raw_bytes))
                continue
            stream, opened = _open_regular(item.root, item.relative_path, item.source)
            if opened.st_size != item.size_bytes:
                stream.close()
                raise ReleaseSourceError(f"Input size changed before archive write: {item.relative_path}")
            reader = _VerifyingReader(stream, item.size_bytes)
            try:
                archive.addfile(_tar_info(name, item.size_bytes, item.mode), reader)
            finally:
                stream.close()
            if reader.size != item.size_bytes or reader.digest.hexdigest() != item.sha256:
                raise ReleaseSourceError(f"Input changed while building archive: {item.relative_path}")


def _write_temp(path: Path, data: bytes) -> Path:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return temp


def build_bundle(
    *,
    upstream_dir: Path,
    ubuntu_dir: Path,
    runtime_dir: Path,
    output_dir: Path,
    linux_report: Path,
    application_commit: str,
    require_complete: bool = False,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", application_commit):
        raise ReleaseSourceError("--application-commit must be exactly 40 hexadecimal characters")
    repo_root = _root_dir(repo_root, "repository root")
    upstream_dir = _root_dir(upstream_dir, "upstream source directory")
    ubuntu_dir = _root_dir(ubuntu_dir, "Ubuntu source directory")
    runtime_dir = _root_dir(runtime_dir, "runtime source directory")
    report_parent = _root_dir(linux_report.parent, "Linux report directory")
    report_path = linux_report.name
    if report_parent != linux_report.parent.resolve(strict=True):
        raise ReleaseSourceError("Linux report path has an unsafe parent")

    version = _load_project_version(repo_root)
    output_abs = Path(os.path.abspath(output_dir))
    parent_chain = list(reversed(output_abs.parents)) + [output_abs]
    for component in parent_chain:
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseSourceError(f"Unsafe output directory parent: {component}")
    for input_root in (upstream_dir, ubuntu_dir, runtime_dir, report_parent):
        if _paths_overlap(output_abs, input_root):
            raise ReleaseSourceError(f"Output directory overlaps a source input: {input_root}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() or output_dir.is_symlink():
        _root_dir(output_dir, "output directory")
    else:
        output_dir.mkdir(parents=True)
    output_dir = output_dir.resolve(strict=True)

    base_name = f"NovaMicSwitch-{version}-Corresponding-Sources"
    archive_path = output_dir / f"{base_name}.tar"
    checksum_path = output_dir / f"{base_name}.tar.sha256"
    index_path = output_dir / "source-bundle-index.json"
    markdown_path = output_dir / "SOURCE-INDEX.md"
    for target in (archive_path, checksum_path, index_path, markdown_path):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ReleaseSourceError(f"Unsafe output target: {target}")

    files: list[InputFile] = []
    paths: set[str] = set()
    upstream_index, upstream_raw = _read_small_json(upstream_dir, "source-index.json", "upstream source index")
    upstream_config, config_raw = _read_small_json(repo_root, "packaging/sources/upstream.json", "upstream build configuration")
    upstream_assets = upstream_index.get("assets")
    config_assets = upstream_config.get("assets")
    if not isinstance(upstream_assets, list) or not isinstance(config_assets, list):
        raise ReleaseSourceError("Upstream index/config has no assets list")
    config_by_id = {item.get("id"): item for item in config_assets if isinstance(item, dict)}
    if len(config_by_id) != len(config_assets):
        raise ReleaseSourceError("Duplicate or invalid asset in upstream build configuration")
    recipe_config = upstream_config.get("qt_ffmpeg_recipes")
    if not isinstance(recipe_config, dict):
        raise ReleaseSourceError("Missing Qt FFmpeg recipe pin in upstream build configuration")
    recipe_id = "qt5-ffmpeg-recipes"
    if recipe_id in config_by_id:
        raise ReleaseSourceError("Duplicate Qt recipe asset configuration")
    config_by_id[recipe_id] = {
        "id": recipe_id,
        "filename": recipe_config.get("archive_filename"),
        "version": "6.12.0",
        "url": f"https://github.com/{recipe_config.get('repository')}/tree/{recipe_config.get('resolved_commit')}",
        "sha256": recipe_config.get("sha256"),
    }
    upstream_summary: list[dict[str, Any]] = []
    asset_ids: set[str] = set()
    for asset in upstream_assets:
        if not isinstance(asset, dict):
            raise ReleaseSourceError("Invalid upstream asset record")
        asset_id = asset.get("asset_id")
        filename = safe_relative(asset.get("filename"), "upstream asset filename")
        if "/" in filename or asset_id in asset_ids:
            raise ReleaseSourceError(f"Invalid or duplicate upstream asset: {asset_id!r}")
        asset_ids.add(asset_id)
        if not isinstance(asset_id, str) or asset_id not in config_by_id:
            raise ReleaseSourceError(f"Upstream asset is missing from pinned build config: {asset_id}")
        config = config_by_id[asset_id]
        if config.get("filename") != filename or config.get("version") != asset.get("version"):
            raise ReleaseSourceError(f"Upstream index/config mismatch for {asset_id}")
        expected = asset.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise ReleaseSourceError(f"Invalid expected SHA-256 for upstream asset {asset_id}")
        item = _add_file(
            files,
            paths,
            upstream_dir,
            filename,
            f"upstream/{filename}",
            "upstream_source_archive",
            config.get("url") or asset.get("url") or asset_id,
            expected_sha256=expected,
            expected_size=asset.get("size_bytes"),
        )
        max_bytes = config.get("max_bytes")
        if max_bytes is not None and item.size_bytes > max_bytes:
            raise ReleaseSourceError(f"Upstream asset exceeds pinned maximum size: {asset_id}")
        expected_config_hash = config.get("sha256")
        if expected_config_hash is not None and item.sha256 != expected_config_hash:
            raise ReleaseSourceError(f"Build config SHA-256 mismatch for {asset_id}")
        if config.get("sha1"):
            sha1_stream, _ = _open_regular(upstream_dir, filename, f"{asset_id} SHA-1")
            sha1 = hashlib.sha1()
            with sha1_stream:
                while chunk := sha1_stream.read(HASH_CHUNK_SIZE):
                    sha1.update(chunk)
            if sha1.hexdigest() != config["sha1"]:
                raise ReleaseSourceError(f"Qt recipe SHA-1 mismatch for {asset_id}")
        if asset.get("manifest"):
            manifest = asset["manifest"]
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise ReleaseSourceError(f"Invalid nested source manifest for {asset_id}")
            _validate_recipe_archive(upstream_dir, filename, manifest["files"], asset_id)
        basis = asset.get("verification_basis", "")
        if asset_id == "qt-everywhere-src":
            basis = (
                "SHA-256 matches packaging/sources/upstream.json pinned official versioned Qt source digest "
                f"{config.get('sha256')}; locally rechecked from the staged archive."
            )
        upstream_summary.append({"asset_id": asset_id, "filename": filename, "version": asset.get("version"), "sha256": item.sha256, "size_bytes": item.size_bytes, "verification_basis": basis})
    if asset_ids != set(config_by_id):
        raise ReleaseSourceError("Upstream source index and pinned build configuration asset sets differ")
    recipe_index = upstream_index.get("assets", [])
    recipe_item = next((a for a in recipe_index if a.get("asset_id") == "qt5-ffmpeg-recipes"), None)
    if recipe_item is None:
        raise ReleaseSourceError("Qt FFmpeg recipe archive is missing")

    _add_file(files, paths, upstream_dir, "source-index.json", "upstream/source-index.json", "source_index", "staged upstream asset index", text=True)
    for rel, name in (("SOURCE-INDEX.md", "SOURCE-INDEX.md"), ("SHA256SUMS", "SHA256SUMS")):
        _add_file(files, paths, upstream_dir, rel, f"upstream/{name}", "source_index_companion", "staged upstream index companion", text=True)
    source_sums = _sha256_sums(_read_small_text(upstream_dir, "SHA256SUMS", "upstream SHA256SUMS"), "upstream SHA256SUMS")
    expected_upstream_sums = {asset["filename"]: asset["sha256"].lower() for asset in upstream_assets}
    if source_sums != expected_upstream_sums:
        raise ReleaseSourceError("Upstream SHA256SUMS does not exactly match source-index.json")

    ubuntu_index, ubuntu_raw = _read_small_json(ubuntu_dir, "ubuntu-source-index.json", "Ubuntu source index")
    source_records = ubuntu_index.get("sources")
    if not isinstance(source_records, list):
        raise ReleaseSourceError("Ubuntu source index has no sources list")
    ubuntu_member_paths: set[str] = set()
    for source in source_records:
        if not isinstance(source, dict) or not isinstance(source.get("files"), list):
            raise ReleaseSourceError("Invalid Ubuntu source package entry")
        for record in source["files"]:
            if not isinstance(record, dict):
                raise ReleaseSourceError("Invalid Ubuntu source file entry")
            rel = safe_relative(record.get("path"), "Ubuntu source file path")
            if not rel.startswith("sources/") or rel in ubuntu_member_paths:
                raise ReleaseSourceError(f"Unsafe or duplicate Ubuntu file path: {rel}")
            ubuntu_member_paths.add(rel)
            filename = safe_relative(record.get("filename"), "Ubuntu source filename")
            if rel.rsplit("/", 1)[-1] != filename:
                raise ReleaseSourceError(f"Ubuntu filename/path mismatch: {rel}")
            sha = record.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
                raise ReleaseSourceError(f"Invalid Ubuntu SHA-256: {rel}")
            _add_file(files, paths, ubuntu_dir, rel, f"ubuntu/{rel}", "ubuntu_source_archive", str((record.get("urls") or [rel])[0]), sha, record.get("size_bytes"))
    ubuntu_sums = _sha256_sums(_read_small_text(ubuntu_dir, "UBUNTU-SHA256SUMS", "Ubuntu SHA256SUMS"), "UBUNTU-SHA256SUMS")
    expected_ubuntu_sums = {record["path"]: record["sha256"].lower() for source in source_records for record in source["files"]}
    if ubuntu_sums != expected_ubuntu_sums:
        raise ReleaseSourceError("UBUNTU-SHA256SUMS does not exactly match the Ubuntu source index")
    _add_file(files, paths, ubuntu_dir, "ubuntu-source-index.json", "ubuntu/ubuntu-source-index.json", "source_index", "Ubuntu source index", text=True)
    _add_file(files, paths, ubuntu_dir, "UBUNTU-SHA256SUMS", "ubuntu/UBUNTU-SHA256SUMS", "source_checksum_list", "Ubuntu SHA256SUMS", text=True)

    runtime_manifest_path = "packaging/licenses/appimage-runtime/manifest.json"
    runtime_manifest, runtime_raw = _read_small_json(repo_root, runtime_manifest_path, "AppImage runtime source manifest")
    runtime_sources = runtime_manifest.get("sources")
    if not isinstance(runtime_sources, list) or len(runtime_sources) != 3:
        raise ReleaseSourceError("Expected exactly three indexed AppImage runtime archives")
    runtime_summary: list[dict[str, Any]] = []
    runtime_root_prefix = "build/runtime-materials/"
    for record in runtime_sources:
        if not isinstance(record, dict):
            raise ReleaseSourceError("Invalid runtime source record")
        archived_path = safe_relative(record.get("archive_path"), "runtime archive path")
        if not archived_path.startswith(runtime_root_prefix):
            raise ReleaseSourceError(f"Unexpected runtime archive path: {archived_path}")
        rel = safe_relative(archived_path[len(runtime_root_prefix) :], "runtime directory archive path")
        filename = safe_relative(record.get("filename"), "runtime archive filename")
        if "/" in filename or rel.rsplit("/", 1)[-1] != filename:
            raise ReleaseSourceError(f"Runtime archive filename/path mismatch: {filename}")
        item = _add_file(files, paths, runtime_dir, rel, f"runtime/{rel}", "appimage_runtime_source_archive", record.get("url", filename), record.get("archive_sha256"), record.get("archive_bytes"))
        runtime_summary.append({"filename": filename, "version": record.get("version"), "sha256": item.sha256, "size_bytes": item.size_bytes, "url": record.get("url")})
    _add_file(files, paths, repo_root, runtime_manifest_path, "runtime/runtime-source-manifest.json", "source_manifest", "AppImage runtime source manifest", text=True)

    notice_summary = _collect_notices(repo_root, files, paths)
    _collect_project_files(repo_root, files, paths)
    files.sort(key=lambda item: item.bundle_path)

    report, report_raw = _read_small_json(report_parent, report_path, "Linux native evidence report")
    coverage = compare_linux_coverage(report, ubuntu_index)
    if require_complete and not coverage["linux_source_coverage_complete"]:
        raise ReleaseSourceError(
            "Linux source coverage is incomplete: "
            f"missing pairs={coverage['missing_exact_pairs_from_ubuntu_index']}, "
            f"unindexed pairs={coverage['ubuntu_index_pairs_not_in_report']}, "
            f"unknown native providers={coverage['unknown_native_provider_rows']}"
        )
    report_sha256 = hashlib.sha256(report_raw).hexdigest()

    project_source_entries = [(item.bundle_path, item.size_bytes) for item in files]
    md_index_path = "SOURCE-INDEX.md"
    json_index_path = "source-bundle-index.json"
    index: dict[str, Any] = {
        "schema_version": 1,
        "bundle_name": f"NovaMicSwitch-{version}-Corresponding-Sources.tar",
        "format": "uncompressed deterministic POSIX USTAR; source archives preserved byte-for-byte",
        "application": {"name": "Nova Mic Switch", "version": version, "commit": application_commit.lower()},
        "scope": "Offline corresponding-source and build-material bundle; not a complete legal-coverage determination or source-to-binary reproducibility attestation.",
        "release_ready": False,
        "release_readiness_note": "Linux source-pair coverage is reported separately; legal review of Qt/Windows, runtime, and other third-party obligations remains pending.",
        "legal_review": "separate review required; Qt/Windows, runtime, and other third-party coverage are not asserted complete by Linux source-index matching",
        "linux_source_coverage": {
            **coverage,
            "linux_report_sha256": report_sha256,
            "ubuntu_source_index_sha256": hashlib.sha256(ubuntu_raw).hexdigest(),
        },
        "unknown_native_provider_rows": coverage["unknown_native_provider_rows"],
        "upstream_source_assets": upstream_summary,
        "appimage_runtime_source_archives": runtime_summary,
        "notice_groups": notice_summary,
        "input_file_count": len(files),
        "input_payload_bytes": sum(item.size_bytes for item in files),
        "files": [item.record() for item in files],
        "bundle_companions": [md_index_path, json_index_path],
        "bundle_index_note": "The files list hashes payload members; this index is included in the tar without a self-hash. Verify the complete archive using its detached .sha256 file.",
    }
    index_bytes = (json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    source_markdown = _source_markdown(index).encode("utf-8")
    estimated_size = preflight_tar_size(project_source_entries + [(md_index_path, len(source_markdown)), (json_index_path, len(index_bytes))])
    index["estimated_tar_bytes"] = estimated_size
    # Re-serialize after adding preflight size. The estimate does not change the payload set.
    index_bytes = (json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    source_markdown = _source_markdown(index).encode("utf-8")
    # The estimate is conservatively computed again because the JSON integer width can change.
    estimated_size = preflight_tar_size(project_source_entries + [(md_index_path, len(source_markdown)), (json_index_path, len(index_bytes))])
    index["estimated_tar_bytes"] = estimated_size
    index_bytes = (json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    source_markdown = _source_markdown(index).encode("utf-8")
    actual_estimate = preflight_tar_size(project_source_entries + [(md_index_path, len(source_markdown)), (json_index_path, len(index_bytes))])
    if actual_estimate != estimated_size or actual_estimate >= MAX_BUNDLE_BYTES:
        raise ReleaseSourceError("Tar size preflight did not converge")

    index_temp = checksum_temp = markdown_temp = archive_temp = None
    try:
        archive_temp = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.tmp")
        if archive_temp.exists() or archive_temp.is_symlink():
            raise ReleaseSourceError(f"Owned temporary path already exists: {archive_temp}")
        _write_tar(archive_temp, files, index_bytes, source_markdown)
        if archive_temp.stat().st_size > MAX_BUNDLE_BYTES:
            raise ReleaseSourceError(f"Built archive exceeds size limit: {archive_temp.stat().st_size}")
        archive_sha, archive_size = _hash_file(archive_path.parent, archive_temp.name, "generated source archive")
        index_temp = _write_temp(index_path, index_bytes)
        markdown_temp = _write_temp(markdown_path, source_markdown)
        checksum_temp = _write_temp(checksum_path, f"{archive_sha}  {archive_path.name}\n".encode("ascii"))
        os.replace(archive_temp, archive_path)
        archive_temp = None
        os.replace(index_temp, index_path)
        index_temp = None
        os.replace(markdown_temp, markdown_path)
        markdown_temp = None
        os.replace(checksum_temp, checksum_path)
        checksum_temp = None
    finally:
        for path in (archive_temp, index_temp, markdown_temp, checksum_temp):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    return {
        "archive": str(archive_path),
        "archive_size_bytes": archive_size,
        "archive_sha256": archive_sha,
        "payload_file_count": len(files),
        "payload_bytes": sum(item.size_bytes for item in files),
        "release_ready": index["release_ready"],
        "linux_source_coverage": coverage,
        "output_dir": str(output_dir),
    }


def _source_markdown(index: dict[str, Any]) -> str:
    app = index["application"]
    coverage = index["linux_source_coverage"]
    lines = [
        "# Corresponding Source Bundle",
        "",
        f"- Application: Nova Mic Switch {app['version']}",
        f"- Application commit: `{app['commit']}`",
        f"- Archive: `{index['bundle_name']}` (uncompressed deterministic POSIX USTAR)",
        f"- Payload entries: {index['input_file_count']}",
        f"- Payload bytes: {index['input_payload_bytes']}",
        f"- Release-ready flag: `{str(index['release_ready']).lower()}`",
        "",
        "This bundle preserves staged upstream and Ubuntu source archives, runtime source inputs, indexed notice texts, and selected project build/source files. It does not assert complete legal coverage or source-to-binary reproducibility.",
        "",
        "## Linux source coverage",
        "",
        f"- System source pairs in report: {coverage['report_system_source_pairs']}",
        f"- Ubuntu indexed pairs: {coverage['ubuntu_index_source_pairs']}",
        f"- Exact pairs missing from Ubuntu index: {len(coverage['missing_exact_pairs_from_ubuntu_index'])}",
        f"- Unknown native provider rows: {coverage['unknown_native_provider_rows']}",
        f"- Linux source coverage complete: `{str(coverage['linux_source_coverage_complete']).lower()}`",
        "",
        "## Upstream source archives",
        "",
        "| Asset | Version | Bytes | SHA-256 | Verification basis |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for item in index["upstream_source_assets"]:
        basis = item["verification_basis"].replace("|", "\\|")
        lines.append(f"| `{item['filename']}` | {item['version']} | {item['size_bytes']} | `{item['sha256']}` | {basis} |")
    lines.extend(["", "## Gaps and limits", "", "- The current Linux report contains unresolved native providers; see `source-bundle-index.json` for counts and exact pair comparisons.", "- Windows/Qt, AppImage runtime, and other license coverage require separate legal review; this archive does not mark those scopes complete.", "- No staged archive is extracted or executed during bundle creation.", ""])
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-dir", type=Path, default=ROOT / "dist/sources")
    parser.add_argument("--ubuntu-dir", type=Path, default=ROOT / "dist/ubuntu-sources")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "build/runtime-materials")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/release-sources")
    parser.add_argument("--linux-report", type=Path, default=ROOT / "build/native-evidence-run-43255344982/native-evidence-linux/report.json")
    parser.add_argument("--application-commit", required=True, help="40-character application commit identifying the build")
    parser.add_argument("--require-complete", action="store_true", help="fail unless all reported Linux system source pairs are indexed and no native provider is unresolved")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = build_bundle(
            upstream_dir=args.upstream_dir,
            ubuntu_dir=args.ubuntu_dir,
            runtime_dir=args.runtime_dir,
            output_dir=args.output_dir,
            linux_report=args.linux_report,
            application_commit=args.application_commit,
            require_complete=args.require_complete,
        )
    except (ReleaseSourceError, OSError) as exc:
        print(f"release-source build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
