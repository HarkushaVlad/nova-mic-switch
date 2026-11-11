#!/usr/bin/env python3
"""Collect actual third-party license texts for a frozen application bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
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
    return result


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


def _collect_licenses(
    output_dir: Path,
    *,
    distributions=None,
    python_license: Path | None = None,
    fetch_python_license: bool = False,
) -> Path:
    """Collect curated and installed license texts with deterministic provenance."""
    try:
        selected = {name: (distributions[name] if distributions is not None else importlib.metadata.distribution(name))
                    for name in COLLECT_DISTRIBUTIONS}
    except (importlib.metadata.PackageNotFoundError, KeyError) as exc:
        raise LicenseCollectionError(f"Required packaged distribution metadata is missing: {exc}") from exc
    for name in QT_DISTRIBUTIONS:
        version = selected[name].version
        if version != QT_VERSION:
            raise LicenseCollectionError(
                f"{name} version {version} does not match curated Qt licenses {QT_VERSION}; update lock and license curation together"
            )

    texts: list[dict[str, object]] = []
    # Curated texts are verbatim and deliberately not fetched during collection/build.
    for group_name in CURATED_GROUPS:
        for item in _curated_texts(group_name):
            texts.append({**item, "destination": f"{group_name}/{item['name']}"})
    package_rows = []
    for distribution in selected.values():
        name, files = _distribution_texts(distribution)
        version = distribution.version
        package_rows.append({
            "name": name,
            "version": version,
            "license_expression": distribution.metadata.get("License-Expression"),
            "license": distribution.metadata.get("License"),
            "notice_files": [relative for relative, _ in files],
        })
        for relative, data in files:
            texts.append({"name": Path(relative).name, "data": data, "version": version,
                          "source_url": None, "destination": f"packages/{relative}"})

    py_data, py_source = _python_license(python_license, fetch_python_license)
    texts.append({"name": "Python-LICENSE.txt", "data": py_data,
                  "version": platform.python_version(), "source_url": py_source,
                  "destination": "Python-LICENSE.txt"})
    for entry in texts:
        data = entry["data"]
        entry["sha256"] = _sha256(data)
        entry["size_bytes"] = len(data)

    manifest = {
        "schema_version": 1,
        "python": {"version": platform.python_version(), "platform": sys.platform,
                   "license_source": py_source},
        "distributions": sorted(package_rows, key=lambda row: row["name"].casefold()),
        "texts": [{key: value for key, value in entry.items() if key != "data"}
                  for entry in sorted(texts, key=lambda row: row["destination"].casefold())],
        "review": "Curated Qt for Python 6.12.0 attribution files include FFmpeg 9.0.1 texts from the Qt Multimedia source tree. This is not a full SBOM or a complete inventory of bundled components. Verify the native release artifact and build provenance, review remaining bundled dependencies and obligations, and do not treat collection as a legal-compliance determination.",
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_root = output_dir.resolve()
    # Replace only generated named files; never remove or clear an existing output directory.
    with tempfile.TemporaryDirectory(prefix=".licenses-", dir=output_dir) as temp_name:
        stage = Path(temp_name)
        for entry in texts:
            relative = Path(str(entry["destination"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise LicenseCollectionError(f"Unsafe generated license destination: {relative}")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry["data"])
        notice = _render_notice(package_rows, manifest["texts"], py_source)
        manifest["generated_files"] = ["THIRD-PARTY-NOTICES.md", "manifest.json"]
        manifest["notice_sha256"] = _sha256(notice.encode("utf-8"))
        (stage / "THIRD-PARTY-NOTICES.md").write_text(notice, encoding="utf-8", newline="\n")
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for source in stage.rglob("*"):
            if source.is_file():
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


def ensure_licenses(output_dir: Path, *, distributions=None, python_license: Path | None = None) -> Path:
    """Use a complete version/hash-matched precollection, otherwise collect locally."""
    output_dir = Path(output_dir)
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("invalid cached manifest")
        python_data = manifest.get("python")
        if not isinstance(python_data, dict) or python_data.get("version") != platform.python_version() \
                or python_data.get("platform") != sys.platform:
            raise ValueError("cached collection version mismatch")
        cached_distributions = manifest.get("distributions")
        if not isinstance(cached_distributions, list) or any(not isinstance(row, dict) for row in cached_distributions):
            raise ValueError("invalid cached distribution list")
        versions = {row.get("name"): row.get("version") for row in cached_distributions}
        for name in COLLECT_DISTRIBUTIONS:
            if versions.get(name) != importlib.metadata.version(name):
                raise ValueError(f"cached distribution version mismatch: {name}")
        entries = manifest["texts"]
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("invalid cached text list")
        curated_expected = {}
        for group_name, group in CURATED_GROUPS.items():
            for curated in _curated_texts(group_name):
                curated_expected[f"{group_name}/{curated['name']}"] = curated
        by_destination = {entry.get("destination"): entry for entry in entries}
        required = set(curated_expected) | {"Python-LICENSE.txt"}
        if not required.issubset(by_destination):
            raise ValueError("cached collection is incomplete")
        for entry in entries:
            destination = entry.get("destination")
            if not isinstance(destination, str):
                raise ValueError("invalid cached destination")
            relative = Path(destination)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe cached destination")
            cached_path = output_dir / relative
            try:
                cached_path.resolve().relative_to(output_dir.resolve())
            except ValueError as exc:
                raise ValueError("cached license path escapes output directory") from exc
            data = cached_path.read_bytes()
            if _sha256(data) != entry.get("sha256"):
                raise ValueError(f"cached license hash mismatch: {relative}")
        for destination, curated in curated_expected.items():
            entry = by_destination[destination]
            if (entry.get("sha256") != _sha256(curated["data"])
                    or entry.get("source_url") != curated["source_url"]
                    or entry.get("version") != curated["version"]):
                raise ValueError(f"cached curated source mismatch: {destination}")
        notice = (output_dir / "THIRD-PARTY-NOTICES.md").read_bytes()
        if _sha256(notice) != manifest.get("notice_sha256"):
            raise ValueError("cached notice hash mismatch")
        if not {"THIRD-PARTY-NOTICES.md", "manifest.json"}.issubset(manifest.get("generated_files", [])):
            raise ValueError("cached generated file list is incomplete")
        return output_dir
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return collect_licenses(output_dir, distributions=distributions, python_license=python_license)


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
        if item.get("group") in CURATED_GROUPS or item["destination"].startswith(tuple(CURATED_GROUPS)):
            source = item.get("source_url") or "installed distribution metadata"
            lines.append(f"- `{item['destination']}` (SHA-256 `{item['sha256']}`; source {source}).")
    lines += ["", "## Remaining attribution review", "",
              "Review all bundled Qt modules, plugins, codecs, FFmpeg/runtime components, bootloader and transitive dependencies for additional notices, source or relinking obligations. These source attribution files do not establish the complete native bundle inventory. Inspect the final platform artifact and build provenance before public release. The collected materials are not a full SBOM or a legal-compliance guarantee. No HIDAPI license choice or project-wide license is made here.", ""]
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
