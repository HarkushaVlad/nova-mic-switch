#!/usr/bin/env python3
"""Collect actual third-party license texts for a frozen application bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import posixpath
import sys
import sysconfig
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
QT_VERSION = "6.12.0"
QT_DISTRIBUTIONS = ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6")
COLLECT_DISTRIBUTIONS = (*QT_DISTRIBUTIONS, "hidapi", "pyinstaller")
CURATED_GROUPS = {
    "qt-6.12.0": {
        "version": QT_VERSION,
        "directory": "packaging/licenses/qt-6.12.0",
        "files": {
            "GPL-3.0-only.txt": "https://raw.githubusercontent.com/pyside/pyside-setup/v6.12.0/LICENSES/GPL-3.0-only.txt",
            "LGPL-3.0-only.txt": "https://raw.githubusercontent.com/pyside/pyside-setup/v6.12.0/LICENSES/LGPL-3.0-only.txt",
            "Qt-GPL-exception-1.0.txt": "https://raw.githubusercontent.com/pyside/pyside-setup/v6.12.0/LICENSES/Qt-GPL-exception-1.0.txt",
        },
    },
    "ffmpeg-9.0.1": {
        "version": "9.0.1",
        "directory": "packaging/licenses/ffmpeg-9.0.1",
        "files": {
            name: f"https://raw.githubusercontent.com/qt/qtmultimedia/v6.12.0/src/3rdparty/ffmpeg/{name}"
            for name in (
                "qt_attribution.json", "LICENSE.LGPL-2.1-or-later.txt", "LICENSE.BSD-2-Clause.txt",
                "LICENSE.BSD-Source-Code.txt", "LICENSE.BSL-1.0.txt", "LICENSE.IJG.txt",
                "LICENSE.ISC.txt", "LICENSE.MIT.txt", "LICENSE.Zlib.txt",
            )
        },
    },
}
MAX_TEXT_BYTES = 8 * 1024 * 1024
PYTHON_LICENSE_URL = "https://raw.githubusercontent.com/python/cpython/v{version}/LICENSE"
MATERIAL_MANIFESTS = {
    "qt-native": {
        "manifest": "packaging/licenses/qt-native/manifest.json",
        "prefix": "packaging/licenses/qt-native/",
        "destination": "qt-native",
        "kind": "Qt source attribution/license/notice material",
    },
    "appimage-runtime": {
        "manifest": "packaging/licenses/appimage-runtime/manifest.json",
        "prefix": "packaging/licenses/appimage-runtime/",
        "destination": "appimage-runtime",
    },
    "appimage-runtime-permissive": {
        "manifest": "packaging/licenses/appimage-runtime/permissive/manifest.json",
        "prefix": "packaging/licenses/appimage-runtime/permissive/",
        "destination": "appimage-runtime/permissive",
    },
    "windows-native": {
        "manifest": "packaging/licenses/windows-native/manifest.json",
        "prefix": "packaging/licenses/windows-native/",
        "destination": "windows-native",
    },
}
APPIMAGE_SOURCE_VERSIONS = {
    "type2-runtime-dd6cebe.tar.gz": "commit dd6cebedcbddde9c82f89b011e8e1d40b6e43868",
    "libfuse-3.15.0.tar.xz": "3.15.0",
    "squashfuse-0.5.2.tar.gz": "0.5.2",
}


class LicenseCollectionError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _curated_texts(group_name: str) -> list[dict[str, object]]:
    group = CURATED_GROUPS[group_name]
    manifest_path = ROOT / group["directory"] / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseCollectionError(f"Cannot read curated {group_name} license manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise LicenseCollectionError(f"Invalid curated {group_name} license manifest")
    if manifest.get("version") != group["version"]:
        raise LicenseCollectionError(f"Curated {group_name} license manifest must be version {group['version']}")
    entries = manifest.get("licenses")
    if not isinstance(entries, list):
        raise LicenseCollectionError("Curated Qt license manifest has no licenses list")
    by_name: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise LicenseCollectionError("Invalid Qt license manifest entry")
        name = entry.get("filename")
        if not isinstance(name, str) or Path(name).name != name or name in by_name:
            raise LicenseCollectionError("Unsafe or duplicate filename in Qt license manifest")
        by_name[name] = entry
    if set(by_name) != set(group["files"]):
        raise LicenseCollectionError(f"{group_name} license manifest entries do not match the curated file set")
    result = []
    for name, source_url in group["files"].items():
        entry = by_name[name]
        if entry.get("source_url") != source_url or entry.get("version") != group["version"]:
            raise LicenseCollectionError(f"Incorrect source or version recorded for {group_name} file {name}")
        path = ROOT / group["directory"] / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LicenseCollectionError(f"Missing curated {group_name} license text {name}: {exc}") from exc
        if not data or _sha256(data) != entry.get("sha256"):
            raise LicenseCollectionError(f"{group_name} license text hash mismatch: {name}")
        result.append({"name": name, "data": data, "version": group["version"],
                       "source_url": source_url, "group": group_name})
    result.append({"name": "manifest.json", "data": manifest_path.read_bytes(),
                   "version": group["version"], "source_url": None,
                   "group": group_name, "destination": f"{group_name}/manifest.json",
                   "material_kind": "provenance manifest", "context": "Curated source-text capture provenance."})
    return result


def _safe_posix_relative(value: object, description: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise LicenseCollectionError(f"Unsafe {description}: {value!r}")
    if posixpath.splitdrive(value)[0] or (len(value) > 1 and value[1] == ":") \
            or any(part in ("", ".", "..") for part in value.split("/")):
        raise LicenseCollectionError(f"Unsafe {description}: {value!r}")
    path = Path(*value.split("/"))
    if path.is_absolute() or path.drive:
        raise LicenseCollectionError(f"Unsafe {description}: {value!r}")
    return path


def _read_material_source(relative: str, allowed_root: Path) -> bytes:
    rel = _safe_posix_relative(relative, "material source path")
    if allowed_root.is_symlink():
        raise LicenseCollectionError(f"Symlink is not allowed for material root: {allowed_root}")
    root = allowed_root.resolve()
    path = allowed_root / rel
    cursor = allowed_root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise LicenseCollectionError(f"Symlink is not allowed in collected material source: {relative}")
    try:
        path.resolve(strict=True).relative_to(root)
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise LicenseCollectionError(f"Material source is missing or escapes its approved root: {relative}: {exc}") from exc
    if not data or len(data) > MAX_TEXT_BYTES:
        raise LicenseCollectionError(f"Material source is empty or exceeds size limit: {relative}")
    return data


def _additional_materials() -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    texts: list[dict[str, object]] = []
    snapshot_inputs: list[dict[str, str]] = []
    destinations = set()
    for group, config in MATERIAL_MANIFESTS.items():
        manifest_rel = config["manifest"]
        manifest_path = ROOT / manifest_rel
        material_root = manifest_path.parent
        manifest_bytes = _read_material_source("manifest.json", material_root)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LicenseCollectionError(f"Invalid {group} material manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise LicenseCollectionError(f"Invalid {group} material manifest root")
        snapshot_inputs.append({"source": manifest_rel, "sha256": _sha256(manifest_bytes)})
        if group == "qt-native":
            archive = manifest.get("qt_source_archive")
            if (manifest.get("schema_version") != 1 or not isinstance(archive, dict)
                    or archive.get("version") != QT_VERSION
                    or archive.get("filename") != "qt-everywhere-src-6.12.0.tar.xz"
                    or archive.get("bytes") != 443394020
                    or archive.get("sha256") != "98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165"):
                raise LicenseCollectionError("Qt native material manifest does not match pinned Qt 6.12.0 source archive")
            rows = manifest.get("copied_text_files")
            if not isinstance(rows, list) or len(rows) != 305:
                raise LicenseCollectionError("Qt native material manifest must contain the captured 305 text files")
        elif group == "appimage-runtime":
            sources = manifest.get("sources")
            if (manifest.get("schema_version") != 1 or not isinstance(sources, list)
                    or {row.get("filename"): row.get("version") for row in sources if isinstance(row, dict)} != APPIMAGE_SOURCE_VERSIONS):
                raise LicenseCollectionError("AppImage runtime manifest does not match pinned source versions")
            rows = manifest.get("copied_texts")
            if not isinstance(rows, list) or not rows:
                raise LicenseCollectionError("AppImage runtime manifest has no copied materials")
        elif group == "windows-native":
            rows = manifest.get("copied_text_files")
            if manifest.get("schema_version") != 1 or not isinstance(rows, list) or len(rows) != 14:
                raise LicenseCollectionError("Windows native material manifest must contain the 14 captured text files")
            components = manifest.get("components")
            if not isinstance(components, list) or not components:
                raise LicenseCollectionError("Windows native material manifest has no component version basis")
            component_versions = {}
            for component in components:
                if (not isinstance(component, dict) or not isinstance(component.get("name"), str)
                        or not isinstance(component.get("version"), str) or not component["name"]
                        or not component["version"]):
                    raise LicenseCollectionError("Invalid Windows native component version record")
                component_versions[component["name"]] = component["version"]
        else:
            rows = manifest.get("materials")
            if not isinstance(rows, list) or len(rows) != 4:
                raise LicenseCollectionError("AppImage permissive manifest must contain four captured notices")
        if not rows:
            raise LicenseCollectionError(f"{group} material manifest contains no materials")
        for row in rows:
            if not isinstance(row, dict):
                raise LicenseCollectionError(f"Invalid material record in {group}")
            if group == "qt-native":
                repo_destination = row.get("destination")
                prefix = config["prefix"]
                if not isinstance(repo_destination, str) or not repo_destination.startswith(prefix):
                    raise LicenseCollectionError("Qt material destination is outside its declared group")
                rel = repo_destination[len(prefix):]
                name = row.get("archive_member_path")
                version = QT_VERSION
                source = f"Qt source archive member {name}; Qt {version}; kinds: {', '.join(row.get('material_kinds', []))}"
                kind = ", ".join(row.get("material_kinds", []))
                size, expected_hash = row.get("bytes"), row.get("sha256")
            elif group == "appimage-runtime":
                repo_destination = row.get("destination")
                prefix = config["prefix"]
                if not isinstance(repo_destination, str) or not repo_destination.startswith(prefix):
                    raise LicenseCollectionError("AppImage runtime destination is outside its declared group")
                rel = repo_destination[len(prefix):]
                name = row.get("original_member_path")
                version = next((src["version"] for src in manifest["sources"] if src["filename"] == row.get("archive")), None)
                if not version:
                    raise LicenseCollectionError("AppImage material references an unknown pinned source archive")
                source = f"{version}; archive member {name}; {row.get('actual_license_statement', 'material statement not specified')}"
                basename = Path(rel).name.lower()
                kind = "build recipe" if "recipe" in rel.lower() or basename in {"makefile", "install-dependencies.sh"} else (
                    "author attribution" if basename == "authors" else "license/notice text")
                size, expected_hash = row.get("bytes"), row.get("sha256")
            elif group == "windows-native":
                repo_destination = row.get("destination")
                prefix = config["prefix"]
                if not isinstance(repo_destination, str) or not repo_destination.startswith(prefix):
                    raise LicenseCollectionError("Windows native material destination is outside its declared group")
                rel = repo_destination[len(prefix):]
                name = row.get("component")
                source = row.get("original_member_or_source_url")
                kind = row.get("role")
                if any(not isinstance(value, str) or not value for value in (name, source, kind)):
                    raise LicenseCollectionError("Windows native material is missing component, source, or role provenance")
                version = next((value for component_name, value in component_versions.items()
                                if name == component_name or name.startswith(f"{component_name} ")), None)
                if version is None:
                    raise LicenseCollectionError(f"Windows native material has no component version basis: {name}")
                size, expected_hash = row.get("bytes"), row.get("sha256")
            else:
                rel = row.get("path")
                _safe_posix_relative(rel, "permissive material destination")
                name = row.get("component")
                version = row.get("version_reference")
                source = f"{name}, version reference {version}; {row.get('license_as_stated')}"
                kind = "license notice; version reference only, runtime minor version not independently verified where noted"
                size, expected_hash = row.get("size_bytes"), row.get("sha256")
            relative_source = _safe_posix_relative(rel, f"{group} material destination")
            source_rel = _safe_posix_relative(rel, f"{group} source path")
            source_base = material_root if group == "appimage-runtime-permissive" else ROOT
            if group != "appimage-runtime-permissive":
                source_rel = _safe_posix_relative(repo_destination, f"{group} repository source")
                source_base = ROOT
                # Strip the repository prefix so the read remains under the exact manifest group root.
                source_rel = _safe_posix_relative(repo_destination[len(config["prefix"]):], f"{group} source path")
                source_base = ROOT / config["prefix"].rstrip("/")
            data = _read_material_source(source_rel.as_posix(), source_base)
            if type(size) is not int or size != len(data) or not isinstance(expected_hash, str) or _sha256(data) != expected_hash:
                raise LicenseCollectionError(f"Declared size or SHA-256 mismatch for {group} material {rel}")
            destination = f"{config['destination']}/{relative_source.as_posix()}"
            if destination in destinations:
                raise LicenseCollectionError(f"Duplicate material destination: {destination}")
            destinations.add(destination)
            texts.append({"name": Path(destination).name, "data": data, "version": str(version),
                          "source_url": str(source), "group": group, "destination": destination,
                          "material_kind": kind, "context": str(name)})
            snapshot_inputs.append({"source": f"{manifest_rel}::{rel}", "sha256": _sha256(data)})

        manifest_destination = f"{config['destination']}/manifest.json"
        if manifest_destination in destinations:
            raise LicenseCollectionError(f"Duplicate material destination: {manifest_destination}")
        destinations.add(manifest_destination)
        texts.append({"name": "manifest.json", "data": manifest_bytes, "version": "1",
                      "source_url": manifest_rel, "group": group, "destination": manifest_destination,
                      "material_kind": "provenance manifest", "context": f"Original {group} capture manifest."})

    for source_path, destination, kind, label in (
        (ROOT / "LICENSE", "LICENSE.txt", "project license", "Nova Mic Switch project MIT license"),
        (ROOT / "packaging/licenses/LIBRARY-REPLACEMENT.md", "LIBRARY-REPLACEMENT.md", "replacement guide", "Local library replacement guide"),
    ):
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            raise LicenseCollectionError(f"Missing required project material {source_path}: {exc}") from exc
        if not data or len(data) > MAX_TEXT_BYTES:
            raise LicenseCollectionError(f"Project material is empty or exceeds size limit: {source_path}")
        rel = source_path.relative_to(ROOT).as_posix()
        texts.append({"name": Path(destination).name, "data": data, "version": "2026",
                      "source_url": rel, "group": "project", "destination": destination,
                      "material_kind": kind, "context": label})
        snapshot_inputs.append({"source": rel, "sha256": _sha256(data)})
    return texts, snapshot_inputs


def _distribution_texts(distribution) -> tuple[str, list[tuple[str, bytes]]]:
    name = distribution.metadata.get("Name", "unknown")
    version = distribution.version
    found = []
    for item in distribution.files or ():
        relative = str(item).replace("\\", "/")
        relative_path = Path(relative)
        basename = relative_path.name
        lowered = basename.lower()
        if not any(token in lowered for token in ("license", "copying", "notice", "copyright")):
            continue
        try:
            path = Path(distribution.locate_file(item))
            if not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if data and len(data) <= MAX_TEXT_BYTES:
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in basename)
            source_parts = (relative_path.name,) if relative_path.is_absolute() else relative_path.parts
            clean_parts = ["".join(c if c.isalnum() or c in "._-" else "_" for c in part)
                           for part in source_parts if part not in (".", "..", "")]
            found.append((f"{name}-{version}/{'/'.join(clean_parts) or safe_name}", data))
    found.sort(key=lambda pair: pair[0].casefold())
    return name, found


def _python_license_candidates() -> Iterable[Path]:
    base = Path(sys.base_prefix)
    candidates = [
        base / "LICENSE.txt",
        base / "share/licenses/python/LICENSE",
        Path("/usr/share/licenses/python/LICENSE"),
    ]
    stdlib = Path(sysconfig.get_path("stdlib"))
    candidates.extend((stdlib.parent / "LICENSE.txt", stdlib.parent / "LICENSE"))
    return candidates


def _python_license(explicit: Path | None, fetch: bool) -> tuple[bytes, str]:
    if explicit is not None:
        try:
            data = explicit.read_bytes()
        except OSError as exc:
            raise LicenseCollectionError(f"Cannot read explicit Python license file {explicit}: {exc}") from exc
        if data:
            return data, "explicit local file"
        raise LicenseCollectionError("Explicit Python license file is empty")
    for path in _python_license_candidates():
        try:
            if path.is_file() and (data := path.read_bytes()):
                return data, "local Python installation"
        except OSError:
            continue
    if fetch:
        version = platform.python_version()
        url = PYTHON_LICENSE_URL.format(version=version)
        request = urllib.request.Request(url, headers={"User-Agent": "nova-mic-switch-license-collector"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(MAX_TEXT_BYTES + 1)
        except Exception as exc:
            raise LicenseCollectionError(f"Could not fetch official CPython license from {url}: {exc}") from exc
        if not data or len(data) > MAX_TEXT_BYTES:
            raise LicenseCollectionError("Downloaded CPython license is empty or exceeds size limit")
        return data, url
    raise LicenseCollectionError(
        "Python license text not found locally. Run scripts/collect_licenses.py --fetch-python-license "
        "before building, or pass --python-license PATH. Network fetching is never implicit."
    )


def _collection_inputs(*, distributions=None, python_license=None, fetch_python_license=False,
                       python_license_override=None):
    try:
        selected = {name: (distributions[name] if distributions is not None else importlib.metadata.distribution(name))
                    for name in COLLECT_DISTRIBUTIONS}
    except (importlib.metadata.PackageNotFoundError, KeyError) as exc:
        raise LicenseCollectionError(f"Required packaged distribution metadata is missing: {exc}") from exc
    for name in QT_DISTRIBUTIONS:
        if selected[name].version != QT_VERSION:
            raise LicenseCollectionError(f"{name} version {selected[name].version} does not match curated Qt licenses {QT_VERSION}")

    texts = []
    for group_name in CURATED_GROUPS:
        texts.extend({**item, "destination": f"{group_name}/{item['name']}"}
                     for item in _curated_texts(group_name))
    extra_texts, extra_snapshots = _additional_materials()
    texts.extend(extra_texts)
    package_rows = []
    for distribution in selected.values():
        name, files = _distribution_texts(distribution)
        version = distribution.version
        package_rows.append({"name": name, "version": version,
                             "license_expression": distribution.metadata.get("License-Expression"),
                             "license": distribution.metadata.get("License"),
                             "notice_files": [relative for relative, _ in files]})
        for relative, data in files:
            texts.append({"name": Path(relative).name, "data": data, "version": version,
                          "source_url": None, "destination": f"packages/{relative}",
                          "group": "distribution", "material_kind": "distribution-provided license or notice text",
                          "context": name})
    if python_license_override is None:
        py_data, py_source = _python_license(Path(python_license) if python_license is not None else None,
                                             fetch_python_license)
    else:
        py_data, py_source = python_license_override
    texts.append({"name": "Python-LICENSE.txt", "data": py_data, "version": platform.python_version(),
                  "source_url": py_source, "destination": "Python-LICENSE.txt", "group": "python",
                  "material_kind": "CPython license", "context": "CPython license text."})
    snapshot_rows = [{"destination": str(row["destination"]), "sha256": _sha256(row["data"])} for row in texts]
    snapshot_rows.extend(extra_snapshots)
    state = {"python": {"version": platform.python_version(), "platform": sys.platform,
                        "license_source": py_source, "license_sha256": _sha256(py_data)},
             "distributions": sorted(package_rows, key=lambda row: row["name"].casefold()),
             "inputs": sorted(snapshot_rows, key=lambda row: row.get("destination", row.get("source", "")).casefold())}
    digest = _sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    destinations = [str(row["destination"]) for row in texts]
    if len(destinations) != len(set(destinations)):
        raise LicenseCollectionError("Duplicate generated license text destination")
    return selected, package_rows, texts, py_source, digest


def _collect_licenses(output_dir: Path, *, distributions=None, python_license: Path | None = None,
                      fetch_python_license: bool = False, python_license_override=None) -> Path:
    """Collect curated and installed license texts with deterministic provenance."""
    _, package_rows, texts, py_source, input_digest = _collection_inputs(
        distributions=distributions, python_license=python_license, fetch_python_license=fetch_python_license,
        python_license_override=python_license_override)
    for entry in texts:
        entry["sha256"] = _sha256(entry["data"])
        entry["size_bytes"] = len(entry["data"])
    manifest = {
        "schema_version": 1,
        "python": {"version": platform.python_version(), "platform": sys.platform, "license_source": py_source},
        "distributions": sorted(package_rows, key=lambda row: row["name"].casefold()),
        "input_snapshot_sha256": input_digest,
        "texts": [{key: value for key, value in entry.items() if key != "data"}
                  for entry in sorted(texts, key=lambda row: row["destination"].casefold())],
        "review": "Collected texts are not a full SBOM, a complete native-component inventory, or a legal-compliance determination. Review platform-specific bundled dependencies and release artifacts separately.",
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_root = output_dir.resolve()
    with tempfile.TemporaryDirectory(prefix=".licenses-", dir=output_dir) as temp_name:
        stage = Path(temp_name)
        for entry in texts:
            relative = _safe_posix_relative(entry["destination"], "generated license destination")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry["data"])
        notice = _render_notice(package_rows, manifest["texts"], py_source)
        manifest["generated_files"] = ["THIRD-PARTY-NOTICES.md", "manifest.json"]
        manifest["notice_sha256"] = _sha256(notice.encode("utf-8"))
        (stage / "THIRD-PARTY-NOTICES.md").write_text(notice, encoding="utf-8", newline="\n")
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for source in stage.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(stage)
            destination = output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                destination.parent.resolve().relative_to(output_root)
            except ValueError as exc:
                raise LicenseCollectionError(f"Output directory contains a symlink escaping its root: {relative}") from exc
            os.replace(source, destination)
    return output_dir

def collect_licenses(
    output_dir: Path,
    *,
    distributions=None,
    python_license: Path | None = None,
    fetch_python_license: bool = False,
) -> Path:
    """Collect curated and installed license texts, reporting filesystem failures cleanly."""
    try:
        return _collect_licenses(output_dir, distributions=distributions,
                                 python_license=python_license, fetch_python_license=fetch_python_license)
    except OSError as exc:
        raise LicenseCollectionError(f"License collection filesystem error: {exc}") from exc


def ensure_licenses(output_dir: Path, *, distributions=None, python_license: Path | None = None,
                    fetch_python_license: bool = False) -> Path:
    """Use a complete current-input-matched precollection or regenerate it offline."""
    output_dir = Path(output_dir)
    try:
        python_license_override = _python_license(
            Path(python_license) if python_license is not None else None, fetch=False)
    except LicenseCollectionError as local_error:
        if python_license is not None:
            raise
        python_license_override = _cached_python_license(output_dir)
        if python_license_override is None:
            if fetch_python_license:
                python_license_override = _python_license(None, fetch=True)
            else:
                raise local_error
    _, package_rows, texts, py_source, expected_snapshot = _collection_inputs(
        distributions=distributions, python_license=python_license, fetch_python_license=False,
        python_license_override=python_license_override)
    expected_entries = []
    for item in texts:
        entry = {key: value for key, value in item.items() if key != "data"}
        entry["sha256"] = _sha256(item["data"])
        entry["size_bytes"] = len(item["data"])
        expected_entries.append(entry)
    expected_by_destination = {entry["destination"]: entry for entry in expected_entries}
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("invalid cached manifest")
        if manifest.get("input_snapshot_sha256") != expected_snapshot:
            raise ValueError("cached source-input snapshot mismatch")
        if manifest.get("python") != {"version": platform.python_version(), "platform": sys.platform,
                                      "license_source": py_source}:
            raise ValueError("cached Python runtime mismatch")
        if manifest.get("distributions") != sorted(package_rows, key=lambda row: row["name"].casefold()):
            raise ValueError("cached distribution metadata mismatch")
        entries = manifest.get("texts")
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("invalid cached text list")
        actual_by_destination = {entry.get("destination"): entry for entry in entries}
        if len(actual_by_destination) != len(entries) or actual_by_destination != expected_by_destination:
            raise ValueError("cached license inventory or provenance mismatch")
        output_root = output_dir.resolve()
        for destination, entry in expected_by_destination.items():
            relative = _safe_posix_relative(destination, "cached destination")
            cached_path = output_dir / relative
            try:
                cached_path.resolve(strict=True).relative_to(output_root)
            except (OSError, ValueError) as exc:
                raise ValueError(f"cached license path escapes output directory: {destination}") from exc
            data = cached_path.read_bytes()
            if len(data) != entry["size_bytes"] or _sha256(data) != entry["sha256"]:
                raise ValueError(f"cached license hash mismatch: {destination}")
        notice = (output_dir / "THIRD-PARTY-NOTICES.md").read_bytes()
        if _sha256(notice) != manifest.get("notice_sha256"):
            raise ValueError("cached notice hash mismatch")
        if manifest.get("generated_files") != ["THIRD-PARTY-NOTICES.md", "manifest.json"]:
            raise ValueError("cached generated file list is incomplete")
        return output_dir
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        try:
            return _collect_licenses(output_dir, distributions=distributions, python_license=python_license,
                                     python_license_override=python_license_override)
        except OSError as exc:
            raise LicenseCollectionError(f"License collection filesystem error: {exc}") from exc


def _cached_python_license(output_dir: Path) -> tuple[bytes, str] | None:
    """Load only a version-matched, internally hash-verified cached Python license."""
    output_dir = Path(output_dir)
    manifest_path = output_dir / "manifest.json"
    if output_dir.is_symlink() or manifest_path.is_symlink():
        return None
    try:
        output_root = output_dir.resolve(strict=True)
        manifest_path.resolve(strict=True).relative_to(output_root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("generated_files") != ["THIRD-PARTY-NOTICES.md", "manifest.json"]):
        return None
    snapshot_hash = manifest.get("input_snapshot_sha256")
    if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64 \
            or any(character not in "0123456789abcdef" for character in snapshot_hash):
        return None
    python_record = manifest.get("python") if isinstance(manifest, dict) else None
    if not isinstance(python_record, dict) or python_record.get("version") != platform.python_version() \
            or python_record.get("platform") != sys.platform:
        return None
    source = python_record.get("license_source")
    permitted_sources = {"explicit local file", "local Python installation",
                         PYTHON_LICENSE_URL.format(version=platform.python_version())}
    if not isinstance(source, str) or source not in permitted_sources:
        return None
    entries = manifest.get("texts")
    if not isinstance(entries, list):
        return None
    matches = [entry for entry in entries if isinstance(entry, dict)
               and entry.get("destination") == "Python-LICENSE.txt"]
    if len(matches) != 1:
        return None
    entry = matches[0]
    if (entry.get("group") != "python" or entry.get("name") != "Python-LICENSE.txt"
            or entry.get("version") != platform.python_version() or entry.get("source_url") != source
            or entry.get("material_kind") != "CPython license" or entry.get("context") != "CPython license text."):
        return None
    size, expected_hash = entry.get("size_bytes"), entry.get("sha256")
    if type(size) is not int or size <= 0 or size > MAX_TEXT_BYTES or not isinstance(expected_hash, str):
        return None
    license_path = output_dir / "Python-LICENSE.txt"
    cursor = output_dir
    try:
        for part in Path("Python-LICENSE.txt").parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        license_path.resolve(strict=True).relative_to(output_root)
        if not license_path.is_file():
            return None
        data = license_path.read_bytes()
    except (OSError, ValueError):
        return None
    if len(data) != size or _sha256(data) != expected_hash:
        return None
    notice_path = output_dir / "THIRD-PARTY-NOTICES.md"
    if notice_path.is_symlink():
        return None
    try:
        notice_path.resolve(strict=True).relative_to(output_root)
        notice = notice_path.read_bytes()
    except (OSError, ValueError):
        return None
    if _sha256(notice) != manifest.get("notice_sha256"):
        return None
    return data, source


def _render_notice(packages, texts, python_source: str) -> str:
    lines = [
        "# Third-party notices", "",
        "This file records license/notice texts collected from the installed distributions listed below. "
        "It is an inventory of collected materials, not a legal-compliance determination or a complete inventory of bundled code.", "",
        "## Runtime", "", f"- Python {platform.python_version()} ({sys.platform}); license source: {python_source}.", "",
        "## Installed distributions", "",
        "The metadata below records upstream license declarations where provided. No project-wide license choice is implied.", "",
    ]
    for package in packages:
        declared = package["license_expression"] or package["license"] or "No license declaration in metadata"
        lines.append(f"- {package['name']} {package['version']}: {declared}.")
        for text in package["notice_files"]:
            lines.append(f"  - Collected text: `{text}`")
    lines += ["", "## Curated Qt and FFmpeg texts", "",
              "Qt for Python 6.12.0 source attribution files include FFmpeg 9.0.1, identified by Qt's attribution record as LGPL-2.1-or-later; that record says the Qt prebuilt binaries omit optional GPL or LGPLv3 components. These files are not a complete inventory of all bundled components.", ""]
    for item in texts:
        group = item.get("group")
        if group in CURATED_GROUPS or group == "qt-native":
            source = item.get("source_url") or "curated Qt source attribution"
            lines.append(f"- `{item['destination']}` ({item.get('material_kind', 'source material')}; SHA-256 `{item['sha256']}`; {source}).")
    lines += ["", "## AppImage runtime source materials", "",
              "The following are captured upstream runtime materials and version references, not evidence that every listed component is linked into every release. Build recipes and AUTHORS records are identified by their material kind rather than represented as license grants.", ""]
    for item in texts:
        if item.get("group") in ("appimage-runtime", "appimage-runtime-permissive"):
            lines.append(f"- `{item['destination']}` ({item.get('material_kind', 'source material')}; context: {item.get('context', 'not stated')}; SHA-256 `{item['sha256']}`; reference: {item.get('source_url')}).")
    lines += ["", "## Windows native dependency source materials", "",
              "The Windows capture preserves CPython external-dependency metadata, upstream notices, Qt provisioning provenance, and Mesa/LLVM source-release materials. Its PE comparison reports byte-identical raw sections for the examined Qt archive and PySide6 wheel DLLs, while whole-file hashes differ due to Authenticode-related differences. This is provenance evidence, not proof of source-to-binary reproducibility, actual retained DLL versions, or complete legal applicability. The captured provisioning recipe and dependency metadata are not themselves license grants.", ""]
    for item in texts:
        if item.get("group") == "windows-native":
            lines.append(f"- `{item['destination']}` ({item.get('material_kind', 'source material')}; component/version basis: {item.get('context', 'not stated')} {item.get('version', '')}; SHA-256 `{item['sha256']}`; source/member: {item.get('source_url')}).")
    lines += ["", "## Project materials", "",
              "Nova Mic Switch's own MIT license applies to project material only and does not relicense any third-party material. The library-replacement guide is practical local-testing guidance, not an additional license grant.", ""]
    for item in texts:
        if item.get("group") == "project":
            lines.append(f"- `{item['destination']}`: {item['context']} (SHA-256 `{item['sha256']}`).")
    lines += ["", "## Remaining attribution review", "",
              "The Qt capture includes Qt's verbatim attribution metadata and license/notice material, including the identified ICU 73.2 notice reference. The runtime permissive capture records zlib 1.3.1 and zstd 1.5.6 evidence; musl 1.2.5 and mimalloc 2.1.7 remain versioned notice candidates whose exact runtime minor versions are unverified. Captured static-runtime notices also describe unresolved dependency notices. Windows actual retained DLL versions and Microsoft Visual C++ redistributable inventory/terms remain pending; the Windows materials do not establish complete legal coverage. Ubuntu package and complete native-bundle coverage also remain outside this collection. Inspect final platform artifacts and provenance before release; this is not a full SBOM or legal-compliance determination.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/licenses")
    parser.add_argument("--python-license", type=Path)
    parser.add_argument("--fetch-python-license", action="store_true")
    args = parser.parse_args(argv)
    try:
        collect_licenses(args.output_dir, python_license=args.python_license,
                         fetch_python_license=args.fetch_python_license)
    except LicenseCollectionError as exc:
        parser.error(str(exc))
    print(f"Collected license texts in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
