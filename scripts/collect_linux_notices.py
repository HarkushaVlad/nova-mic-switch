#!/usr/bin/env python3
"""Collect verbatim Ubuntu package notices for native system libraries in a bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

import native_report


MAX_TEXT_BYTES = 5 * 1024 * 1024
NOTICE_REFERENCE = re.compile(r"/usr/share/common-licenses/([A-Za-z0-9.+_-]+)")
NATIVE_TYPES = {"BINARY", "EXTENSION", "EXECUTABLE"}
SYSTEM_PREFIXES = ("/usr/lib/", "/lib/", "/usr/lib64/", "/lib64/")


class LinuxNoticeError(ValueError):
    """Raised when native-package notice evidence cannot be safely established."""


def _validate_relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or
            any(ord(char) < 32 or ord(char) == 127 for char in value) or
            re.match(r"^[A-Za-z]:", value)):
        raise LinuxNoticeError("Unsafe notice destination path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise LinuxNoticeError(f"Unsafe notice destination path: {value!r}")
    return path.as_posix()


def _inside_system_path(path: Path) -> bool:
    value = path.as_posix()
    return value.startswith(SYSTEM_PREFIXES)


def _source_file(value: str) -> Path:
    source = Path(value)
    if not source.is_absolute() or not _inside_system_path(source):
        raise LinuxNoticeError(f"Native system-library source is outside supported system paths: {value!r}")
    try:
        resolved = source.resolve(strict=True)
        if not resolved.is_file():
            raise LinuxNoticeError(f"Native system-library source is not a regular file: {value!r}")
    except OSError as exc:
        raise LinuxNoticeError(f"Native system-library source is unavailable: {value!r}") from exc
    return resolved


def _read_notice(
    stage: Path,
    entry: dict,
    source: Path,
    allowed_root: Path,
    references: list[str] | None = None,
) -> tuple[dict, bytes]:
    relative = _validate_relative(entry.get("path"))
    if not relative.startswith("notices/package/"):
        raise LinuxNoticeError(f"Notice is outside the package notice directory: {relative}")
    path = stage / relative
    try:
        current = stage
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise LinuxNoticeError(f"Notice path traverses a symlink: {relative}")
        if path.is_symlink() or not path.is_file():
            raise LinuxNoticeError(f"Notice is not a regular staged file: {relative}")
        size = path.stat().st_size
        if size <= 0 or size > MAX_TEXT_BYTES:
            raise LinuxNoticeError(f"Notice has invalid size: {relative}")
        data = path.read_bytes()
    except OSError as exc:
        raise LinuxNoticeError(f"Could not read staged notice: {relative}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry.get("sha256") or digest != native_report._sha256(path):
        raise LinuxNoticeError(f"Notice SHA-256 mismatch: {relative}")
    try:
        resolved_source = source.resolve(strict=True)
        resolved_root = allowed_root.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
        if not resolved_source.is_file() or native_report._sha256(resolved_source) != digest:
            raise LinuxNoticeError(f"Notice does not match its Ubuntu source text: {relative}")
    except (OSError, ValueError) as exc:
        raise LinuxNoticeError(f"Notice source is unavailable or outside its approved root: {source}") from exc
    if not resolved_source.is_file():
        raise LinuxNoticeError(f"Notice does not match its Ubuntu source text: {relative}")
    metadata = {
        "path": relative,
        "sha256": digest,
        "size": len(data),
        "source": str(source),
        "resolved_source": str(resolved_source),
    }
    if references is not None:
        metadata["references"] = sorted(set(references))
    return metadata, data


KNOWN_COMMON_ALIASES = {"GPL-2.0": "GPL-2", "GPL-3.0": "GPL-3", "LGPL-2.0": "LGPL-2"}


def _resolve_common_license(reference: str) -> tuple[str, Path]:
    root = Path(native_report.COMMON_LICENSE_ROOT)
    # Prefer an exact existing Ubuntu canonical filename, then strip sentence punctuation.
    candidates = [reference]
    stripped = reference.rstrip(".")
    if stripped != reference:
        candidates.append(stripped)
    alias = KNOWN_COMMON_ALIASES.get(stripped)
    if alias and alias not in candidates:
        candidates.append(alias)
    for candidate in candidates:
        path = root / candidate
        if path.is_file():
            return candidate, path
    raise LinuxNoticeError(f"Referenced common-license text is unavailable: {reference}")


def _package_notices(provider: dict, stage: Path, package: str) -> tuple[list[dict], bytes]:
    if provider.get("notice_status") != "copied" or not provider.get("noticefiles"):
        raise LinuxNoticeError(f"No copyright notice was collected for system package {package}")
    entries = provider["noticefiles"]
    copyright_entries = [entry for entry in entries if Path(str(entry.get("path", ""))).name.endswith("-copyright.txt")]
    if len(copyright_entries) != 1:
        raise LinuxNoticeError(f"Expected exactly one copyright notice for system package {package}")
    copyright_source = Path(native_report.SYSTEM_DOCS_ROOT) / package.split(":", 1)[0] / "copyright"
    copyright_meta, copyright_data = _read_notice(
        stage, copyright_entries[0], copyright_source, Path(native_report.SYSTEM_DOCS_ROOT)
    )
    text = copyright_data.decode("utf-8", errors="replace")
    referenced = sorted(set(NOTICE_REFERENCE.findall(text)))
    by_path = {}
    for entry in entries:
        relative = _validate_relative(entry.get("path"))
        by_path.setdefault(relative, []).append(entry)
    checked = [copyright_meta]
    reference_groups = {}
    for raw_reference in referenced:
        canonical_name, source = _resolve_common_license(raw_reference)
        filename = f"{native_report._safe_notice_name(package)}-common-{native_report._safe_notice_name(raw_reference)}.txt"
        relative = f"notices/package/{filename}"
        resolved_source = source.resolve(strict=True)
        group = reference_groups.setdefault(relative, {"source": source, "resolved_source": resolved_source, "references": set()})
        if group["resolved_source"] != resolved_source:
            raise LinuxNoticeError(f"Distinct common-license texts collide at notice path: {relative}")
        group["references"].update((raw_reference, canonical_name))

    for relative, group in sorted(reference_groups.items()):
        source = group["source"]
        candidates = by_path.get(relative, [])
        entry = None
        if candidates:
            hashes = {candidate.get("sha256") for candidate in candidates}
            if len(hashes) != 1:
                raise LinuxNoticeError(f"Conflicting notice hashes collide at path: {relative}")
            entry = candidates[0]
        if entry is None:
            # native_report intentionally only copies exact paths; stage the verified canonical Ubuntu file here.
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            entry = {"path": relative, "sha256": native_report._sha256(destination)}
        meta, _data = _read_notice(
            stage,
            entry,
            source,
            Path(native_report.COMMON_LICENSE_ROOT),
            sorted(group["references"]),
        )
        checked.append(meta)
    expected_paths = {item["path"] for item in checked}
    supplied_paths = set(by_path)
    # Reject provider-supplied extras rather than silently accepting unreferenced or misattributed text.
    if not supplied_paths <= expected_paths:
        raise LinuxNoticeError(f"Unexpected or missing notice text for system package {package}")
    if len(expected_paths) != len(checked):
        raise LinuxNoticeError(f"Duplicate notice path returned for system package {package}")
    return checked, copyright_data


def collect_linux_notices(toc_rows, output_dir: Path) -> dict:
    """Collect Ubuntu notices for native system-library TOC rows, without bundling binaries."""
    output_dir = Path(output_dir)
    absolute_output = Path(os.path.abspath(output_dir))
    current = Path(absolute_output.anchor)
    if current.is_symlink():
        raise LinuxNoticeError("Output directory must not be a symlink")
    for part in absolute_output.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise LinuxNoticeError("Output path must not traverse symlinks")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise LinuxNoticeError("Output path must be a real directory")
    output_dir = output_dir.resolve()

    rows = []
    seen_destinations = set()
    seen_sources = set()
    scope_skips = []
    for row in toc_rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            raise LinuxNoticeError("PyInstaller TOC rows must be three-tuples")
        destination, original, kind = row
        if not all(isinstance(value, str) for value in row):
            raise LinuxNoticeError("PyInstaller TOC fields must be strings")
        if kind not in NATIVE_TYPES:
            continue
        dest = _validate_relative(destination)
        if Path(original).is_absolute() and not _inside_system_path(Path(original)):
            scope_skips.append({"destination": dest, "source": original, "reason": "outside supported Linux system-library paths; covered by existing wheel/Python groups or out of scope"})
            continue
        # Only the four explicitly supported system roots are eligible for Ubuntu ownership lookup.
        if not _inside_system_path(Path(original)):
            scope_skips.append({"destination": dest, "source": original, "reason": "outside supported Linux system-library paths; covered by existing wheel/Python groups or out of scope"})
            continue
        source = _source_file(original)
        source_key = os.path.normcase(str(source))
        if (dest, source_key) in seen_sources:
            continue
        if dest in seen_destinations and all(existing[0] != dest or existing[1] != source_key for existing in rows):
            raise LinuxNoticeError(f"Multiple native sources map to the same bundle destination: {dest}")
        seen_destinations.add(dest)
        seen_sources.add((dest, source_key))
        rows.append((dest, source, original, kind))
    rows.sort(key=lambda item: (item[0], str(item[1])))

    if not rows:
        manifest = {"schema": 1, "scope_skips": scope_skips, "source_packages": [], "binary_providers": [], "native_inputs": [], "texts": []}
        return _publish(output_dir, manifest, {})

    stage = Path(tempfile.mkdtemp(prefix=".linux-notice-stage-", dir=output_dir))
    try:
        notice_cache: dict[str, list[dict]] = {}
        packages: dict[tuple[str, str], dict] = {}
        inputs = []
        package_texts: dict[str, dict[str, dict]] = {}
        providers = []
        for dest, source, original, kind in rows:
            host = native_report._host_provider(source, stage, notice_cache)
            if not host:
                raise LinuxNoticeError(f"Could not resolve Ubuntu package owner/version for native library: {original}")
            binary = host.get("provider") or {}
            package = binary.get("name")
            binary_version = binary.get("version")
            source_package = host.get("source_package")
            source_version = host.get("source_version")
            if binary.get("kind") != "system-package" or not all(isinstance(v, str) and v for v in (package, binary_version, source_package, source_version)):
                raise LinuxNoticeError(f"Incomplete Ubuntu binary/source package identity for {original}")
            notices, copyright_bytes = _package_notices(host, stage, package)
            package_key = (source_package, source_version)
            packages.setdefault(package_key, {"name": source_package, "version": source_version, "binary_packages": []})
            if package not in packages[package_key]["binary_packages"]:
                packages[package_key]["binary_packages"].append(package)
            package_texts.setdefault(package, {})
            for item in notices:
                package_texts[package][item["path"]] = item
            providers.append({"destination": dest, "provider": binary, "source_package": source_package, "source_version": source_version})
            inputs.append({"destination": dest, "source": str(source), "original_source": original, "type": kind, "sha256": native_report._sha256(source)})

        texts = sorted((item for group in package_texts.values() for item in group.values()), key=lambda item: item["path"])
        # Human-readable index uses only the copied text evidence and clearly limits its scope.
        index = [
            "# Linux system-package notices",
            "",
            "These verbatim copyright and referenced common-license texts were captured from Ubuntu package metadata for native system libraries in this build.",
            "This build capture is not a complete license inventory of the application and does not replace the separately provided corresponding-source index.",
            "Python and wheel components are handled by their existing license groups; this index covers only native inputs under supported system library paths.",
            "",
            "## Package texts",
            "",
        ]
        for item in texts:
            index.append(f"- `{item['path']}` (SHA-256 `{item['sha256']}`)")
        manifest = {
            "schema": 1,
            "scope_skips": sorted(scope_skips, key=lambda item: (item["destination"], item["source"])),
            "source_packages": [dict(value, binary_packages=sorted(value["binary_packages"])) for _key, value in sorted(packages.items())],
            "binary_providers": sorted(providers, key=lambda item: (item["destination"], item["provider"]["name"])),
            "native_inputs": sorted(inputs, key=lambda item: (item["destination"], item["source"])),
            "texts": texts,
        }
        generated = {item["path"]: (stage / item["path"]).read_bytes() for item in texts}
        generated["THIRD-PARTY-NOTICES.md"] = ("\n".join(index) + "\n").encode("utf-8")
        return _publish(output_dir, manifest, generated)
    finally:
        shutil.rmtree(stage)


def _publish(output_dir: Path, manifest: dict, generated: dict[str, bytes]) -> dict:
    manifest_path = output_dir / "manifest.json"
    owned = {}
    old_manifest = None
    if manifest_path.is_symlink():
        raise LinuxNoticeError("Existing manifest is a symlink; refusing cache reuse")
    if manifest_path.exists():
        try:
            if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_TEXT_BYTES:
                raise LinuxNoticeError("Existing manifest is not a safe regular file")
            old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LinuxNoticeError("Existing manifest is corrupt; refusing cache reuse") from exc
        if not isinstance(old_manifest, dict) or old_manifest.get("schema") != 1:
            raise LinuxNoticeError("Existing manifest has an unsupported schema; refusing cache reuse")
        inventory = old_manifest.get("owned_files")
        if not isinstance(inventory, list):
            raise LinuxNoticeError("Existing manifest lacks a validated ownership inventory")
        for record in inventory:
            if not isinstance(record, dict):
                raise LinuxNoticeError("Existing manifest contains an invalid ownership record")
            relative = _validate_relative(record.get("path"))
            if relative != "THIRD-PARTY-NOTICES.md" and not relative.startswith("notices/package/"):
                raise LinuxNoticeError(f"Existing manifest claims an unsafe output path: {relative}")
            if relative in owned or not isinstance(record.get("sha256"), str):
                raise LinuxNoticeError(f"Existing manifest contains duplicate/invalid ownership: {relative}")
            target = output_dir / relative
            _check_output_path(output_dir, relative)
            if target.is_symlink() or not target.is_file() or native_report._sha256(target) != record["sha256"]:
                raise LinuxNoticeError(f"Previously generated file is missing or tampered: {relative}")
            owned[relative] = record["sha256"]

    current_records = [
        {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for relative, data in sorted(generated.items())
    ]
    for record in current_records:
        relative = record["path"]
        _check_output_path(output_dir, relative)
        target = output_dir / relative
        if target.exists() or target.is_symlink():
            if relative not in owned:
                raise LinuxNoticeError(f"Refusing to overwrite unowned output: {target}")
    combined_owned = dict(owned)
    combined_owned.update({record["path"]: record["sha256"] for record in current_records})
    manifest["generated_files"] = current_records
    manifest["owned_files"] = [
        {"path": path, "sha256": digest} for path, digest in sorted(combined_owned.items())
    ]
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    all_files = dict(generated)

    unchanged = old_manifest is not None and manifest_path.read_bytes() == manifest_data
    if unchanged and all(
        (output_dir / relative).is_file() and native_report._sha256(output_dir / relative) == record["sha256"]
        for relative, record in ((record["path"], record) for record in current_records)
    ):
        return {"manifest": str(manifest_path), "files": [str(output_dir / path) for path in sorted((*all_files, "manifest.json"))], "cache_hit": True}

    # Publish text files individually and atomically; the manifest is the final commit marker.
    for relative, data in sorted(all_files.items()):
        _atomic_write(output_dir, relative, data, replace=relative in owned)
    _atomic_write(output_dir, "manifest.json", manifest_data, replace=manifest_path.exists())
    return {"manifest": str(manifest_path), "files": [str(output_dir / path) for path in sorted((*all_files, "manifest.json"))], "cache_hit": False}


def _check_output_path(output_dir: Path, relative: str) -> None:
    _validate_relative(relative)
    target = output_dir / relative
    current = output_dir
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise LinuxNoticeError(f"Refusing output path through symlink: {current}")
    if target.is_symlink():
        raise LinuxNoticeError(f"Refusing output symlink: {target}")


def _atomic_write(output_dir: Path, relative: str, data: bytes, *, replace: bool) -> None:
    _check_output_path(output_dir, relative)
    target = output_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    _check_output_path(output_dir, relative)
    if target.exists() and not replace:
        raise LinuxNoticeError(f"Refusing to overwrite unowned output: {target}")
    fd, temporary = tempfile.mkstemp(prefix=".linux-notice-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
