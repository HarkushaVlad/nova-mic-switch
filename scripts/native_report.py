#!/usr/bin/env python3
"""Create bounded provenance and license evidence for a PyInstaller bundle."""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_TEXT_BYTES = 5 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 5
SYSTEM_DOCS_ROOT = Path("/usr/share/doc")
COMMON_LICENSE_ROOT = Path("/usr/share/common-licenses")
NATIVE_SUFFIXES = (".dll", ".pyd", ".exe")
FFMPEG_LIBRARIES = {
    "avutil": "avutil",
    "avcodec": "avcodec",
    "avformat": "avformat",
    "swresample": "swresample",
    "swscale": "swscale",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_native(path: Path) -> bool:
    name = path.name.lower()
    return name == "novamicswitch" or name.endswith(NATIVE_SUFFIXES) or bool(re.search(r"\.so(?:\..*)?$", name))


def _normalize_relative(value: str) -> str:
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return "/".join(part for part in value.split("/") if part not in ("", "."))


def _source_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _walk_toc(value):
    if isinstance(value, (tuple, list)):
        if (
            len(value) == 3
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], str)
            and value[2] in {"BINARY", "EXTENSION", "EXECUTABLE"}
        ):
            yield value[0], value[1], value[2]
        for child in value:
            yield from _walk_toc(child)


def _toc_priority(path: Path) -> int | None:
    name = path.name.casefold()
    if name.startswith("analysis-"):
        return 0
    if name.startswith("collect-"):
        return 1
    if name.startswith(("exe-", "pkg-", "bundle-")):
        return 2
    return None


def _canonical_destination(value: str) -> str:
    key = _normalize_relative(value).casefold()
    return key[len("_internal/"):] if key.startswith("_internal/") else key


def _read_toc_map(workpath: Path) -> tuple[dict[str, dict[int, set[Path]]], list[str]]:
    entries: dict[str, dict[int, set[Path]]] = {}
    issues = []
    for toc_path in sorted(workpath.rglob("*.toc")):
        priority = _toc_priority(toc_path)
        if priority is None:
            continue
        try:
            if toc_path.stat().st_size > MAX_TEXT_BYTES:
                issues.append(f"Skipped oversized PyInstaller TOC: {toc_path}")
                continue
            toc = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError, UnicodeError) as exc:
            issues.append(f"Could not parse PyInstaller TOC {toc_path}: {exc}")
            continue
        for destination, source, _typecode in _walk_toc(toc):
            key = _canonical_destination(destination)
            if key:
                entries.setdefault(key, {}).setdefault(priority, set()).add(_source_path(source, workpath))
    return entries, issues


def _toc_sources(relative_path: str, toc_map: dict[str, dict[int, set[Path]]]) -> set[Path]:
    target = _canonical_destination(relative_path)
    exact = toc_map.get(target)
    if exact:
        preferred_priority = min(exact)
        return set(exact[preferred_priority])

    basename = target.rsplit("/", 1)[-1]
    candidates = set()
    for key, priorities in toc_map.items():
        if key.rsplit("/", 1)[-1] == basename:
            preferred_priority = min(priorities)
            candidates.update(priorities[preferred_priority])
    return candidates


def _wheel_owners() -> dict[str, tuple[str, str]]:
    owners = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or "unknown"
        version = distribution.version or "unknown"
        for item in distribution.files or ():
            try:
                installed_path = Path(distribution.locate_file(item)).resolve()
                owners[_path_key(installed_path)] = (name, version)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
    return owners


def _path_key(path: Path) -> str:
    value = os.path.normcase(str(path.resolve()))
    return value.casefold() if os.name == "nt" else value


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_python_runtime_source(path: Path) -> bool:
    if any(part.casefold() in {"site-packages", "dist-packages"} for part in path.parts):
        return False
    for prefix in {Path(sys.base_prefix), Path(sys.prefix)}:
        if not _under(path, prefix):
            continue
        try:
            relative_parts = path.resolve().relative_to(prefix.resolve()).parts
        except (OSError, ValueError):
            continue
        normalized = tuple(part.casefold() for part in relative_parts)
        if os.name == "nt":
            if normalized and normalized[0] in {"lib", "dlls"}:
                return True
            if len(normalized) == 1 and re.fullmatch(r"python3[0-9]*\.dll", normalized[0]):
                return True
        else:
            if len(normalized) >= 2 and normalized[0] == "lib" and re.fullmatch(r"python3(?:\.\d+)?", normalized[1]):
                return True
            if normalized and re.fullmatch(r"libpython3(?:\.\d+)*\.so(?:\..*)?", normalized[-1]):
                return True
    return False


def _run_dpkg(arguments: list[str]) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["dpkg-query", *arguments], check=False, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    output_size = len(result.stdout.encode("utf-8", errors="replace")) + len(result.stderr.encode("utf-8", errors="replace"))
    if output_size > MAX_TEXT_BYTES:
        return "", "dpkg-query output exceeded the 5 MiB limit"
    return (result.stdout if result.returncode == 0 else ""), result.stderr.strip()


def _package_for_path(path: Path) -> str | None:
    if not (str(path).startswith("/usr/lib/") or str(path).startswith("/lib/")):
        return None
    for candidate in dict.fromkeys((path, path.resolve())):
        output, _error = _run_dpkg(["-S", str(candidate)])
        for line in output.splitlines():
            owner, separator, _owned_path = line.partition(": ")
            if separator and owner:
                return owner.split(",", 1)[0].strip()
    return None


def _safe_notice_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9.+_-]+", "_", value).strip("._") or "unknown"


def _copy_host_notices(package: str, output_dir: Path, notice_cache: dict[str, list[dict]]) -> list[dict]:
    if package in notice_cache:
        return notice_cache[package]
    binary_package = package.split(":", 1)[0]
    copyright_path = SYSTEM_DOCS_ROOT / binary_package / "copyright"
    notices = []

    def copy_notice(source: Path, suffix: str):
        try:
            if not source.is_file() or source.stat().st_size > MAX_TEXT_BYTES:
                return
            relative = Path("notices") / "package" / f"{_safe_notice_name(package)}-{suffix}"
            destination = output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            notices.append({"path": relative.as_posix(), "sha256": _sha256(destination)})
        except OSError:
            return

    copy_notice(copyright_path, "copyright.txt")
    try:
        if copyright_path.is_file() and copyright_path.stat().st_size <= MAX_TEXT_BYTES:
            text = copyright_path.read_text(encoding="utf-8", errors="replace")
            for license_name in sorted(set(re.findall(r"/usr/share/common-licenses/([A-Za-z0-9.+_-]+)", text))):
                copy_notice(COMMON_LICENSE_ROOT / license_name, f"common-{_safe_notice_name(license_name)}.txt")
    except OSError:
        pass
    notice_cache[package] = notices
    return notices


def _host_provider(path: Path, output_dir: Path, notice_cache: dict[str, list[dict]]) -> dict | None:
    package = _package_for_path(path)
    if not package:
        return None
    output, error = _run_dpkg([
        "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n", package,
    ])
    fields = output.rstrip("\n").split("\t") if output else []
    if len(fields) != 4:
        return None
    binary_name, binary_version, source_name, source_version = fields
    notices = _copy_host_notices(binary_name, output_dir, notice_cache)
    return {
        "provider": {"name": binary_name, "version": binary_version or None, "kind": "system-package"},
        "source_package": source_name or binary_name,
        "source_version": source_version or None,
        "noticefiles": notices,
        "notice_status": "copied" if notices else "missing",
        "resolution_detail": error or None,
    }


def _provider_for(source: Path | None, relative_path: str, wheel_owners: dict[str, tuple[str, str]],
                  output_dir: Path, notice_cache: dict[str, list[dict]]) -> dict:
    basename = Path(relative_path).name.lower()
    if basename in ("novamicswitch", "novamicswitch.exe"):
        return {
            "provider": {"name": "Nova Mic Switch", "version": "project", "kind": "project-executable"},
            "source_package": "nova-mic-switch", "source_version": None, "noticefiles": [],
            "notice_status": "not-collected",
        }
    if source is not None:
        key = _path_key(source)
        if key in wheel_owners:
            name, version = wheel_owners[key]
            return {
                "provider": {"name": name, "version": version, "kind": "python-wheel"},
                "source_package": name, "source_version": version, "noticefiles": [],
                "notice_status": "not-collected",
            }
        if sys.platform.startswith("linux"):
            host = _host_provider(source, output_dir, notice_cache)
            if host:
                return host
        if _is_python_runtime_source(source):
            return {
                "provider": {"name": "Python runtime", "version": sys.version.split()[0], "kind": "python-runtime"},
                "source_package": None, "source_version": None, "noticefiles": [],
                "notice_status": "not-collected",
            }
        source_posix = source.as_posix().casefold()
        if "pyinstaller" in source_posix and "bootloader" in source_posix:
            return {
                "provider": {"name": "PyInstaller bootloader", "version": None, "kind": "pyinstaller-bootloader"},
                "source_package": "pyinstaller", "source_version": None, "noticefiles": [],
                "notice_status": "not-collected",
            }
    return {
        "provider": {"name": None, "version": None, "kind": "unresolved"},
        "source_package": None, "source_version": None, "noticefiles": [],
        "notice_status": "unresolved",
    }


def _decode_c_string(value) -> str | None:
    if not value:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _ffmpeg_evidence(bundle: Path) -> list[dict]:
    binaries = [path for path in bundle.rglob("*") if path.is_file() and path.suffix.lower() != ".exe"]
    results = []
    dll_handles = []
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for directory in sorted({path.parent for path in binaries if path.suffix.lower() == ".dll"}):
            try:
                dll_handles.append(os.add_dll_directory(str(directory)))
            except OSError:
                continue
    try:
        for prefix, symbol_prefix in FFMPEG_LIBRARIES.items():
            candidates = [
                path for path in binaries
                if path.name.lower().startswith((prefix.lower(), f"lib{prefix.lower()}"))
                and (".so" in path.name.lower() or path.suffix.lower() in (".dll", ".dylib"))
            ]
            for library_path in sorted(candidates):
                item = {"path": library_path.relative_to(bundle).as_posix(), "load_error": None}
                try:
                    library = ctypes.CDLL(str(library_path))
                    version_function = getattr(library, f"{symbol_prefix}_version")
                    version_function.argtypes = []
                    version_function.restype = ctypes.c_uint
                    configuration_function = getattr(library, f"{symbol_prefix}_configuration")
                    configuration_function.argtypes = []
                    configuration_function.restype = ctypes.c_char_p
                    license_function = getattr(library, f"{symbol_prefix}_license")
                    license_function.argtypes = []
                    license_function.restype = ctypes.c_char_p
                    version_number = int(version_function())
                    item.update({
                        "version_number": version_number,
                        "version": f"{version_number >> 16}.{(version_number >> 8) & 255}.{version_number & 255}",
                        "configuration": _decode_c_string(configuration_function()),
                        "license": _decode_c_string(license_function()),
                    })
                except Exception as exc:
                    item["load_error"] = f"{type(exc).__name__}: {exc}"
                results.append(item)
    finally:
        for handle in dll_handles:
            handle.close()
    return results


def build_report(bundle: Path, workpath: Path, output_dir: Path) -> dict:
    bundle = bundle.resolve()
    workpath = workpath.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    toc_map, toc_issues = _read_toc_map(workpath)
    wheel_owners = _wheel_owners()
    notice_cache: dict[str, list[dict]] = {}
    rows = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file() and _is_native(item)):
        relative = path.relative_to(bundle).as_posix()
        sources = _toc_sources(relative, toc_map)
        source = next(iter(sources)) if len(sources) == 1 else None
        row = {
            "relative_bundle_path": relative,
            "sha256": _sha256(path),
            "original_input_path": str(source) if source is not None else None,
            "source_mapping": "resolved" if source is not None else "ambiguous" if sources else "unresolved",
            "source_candidates": sorted(str(candidate) for candidate in sources) if len(sources) != 1 else [],
            "source_sha256": _sha256(source) if source is not None and source.is_file() else None,
            "source_hash_differs": _sha256(path) != _sha256(source) if source is not None and source.is_file() else None,
        }
        row.update(_provider_for(source, relative, wheel_owners, output_dir, notice_cache))
        rows.append(row)
    report = {
        "schema_version": 1,
        "bundle": str(bundle),
        "workpath": str(workpath),
        "native_files": rows,
        "ffmpeg": _ffmpeg_evidence(bundle),
        "unresolved": [
            row["relative_bundle_path"] for row in rows
            if row["provider"]["kind"] == "unresolved" or row["source_mapping"] != "resolved"
            or row.get("notice_status") == "missing"
        ],
        "inventory_issues": toc_issues,
        "noticefiles": sorted(
            {notice["path"]: notice for values in notice_cache.values() for notice in values}.values(),
            key=lambda notice: notice["path"],
        ),
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--workpath", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.bundle.is_dir():
        parser.error(f"bundle is not a directory: {args.bundle}")
    if not args.workpath.is_dir():
        parser.error(f"PyInstaller workpath is not a directory: {args.workpath}")
    report = build_report(args.bundle, args.workpath, args.output_dir)
    print(f"Native evidence report: {args.output_dir / 'report.json'}")
    print(f"Native files: {len(report['native_files'])}; unresolved provenance: {len(report['unresolved'])}")
    print(f"FFmpeg libraries inspected: {len(report['ffmpeg'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
