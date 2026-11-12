#!/usr/bin/env python3
"""Collect and verify facts about retained Microsoft runtime DLLs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile


SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 5 * 1024 * 1024
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
MICROSOFT_RUNTIME_DLLS = {
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
    "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "concrt140.dll",
}
MSVC_LOOKING_DLL = re.compile(r"^(?:vcruntime|msvcp|concrt).+\.dll$", re.IGNORECASE)
TEMPLATE_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|PLACEHOLDER|INSERT TERMS HERE)\b", re.IGNORECASE)


class RuntimeTermsError(RuntimeError):
    """Raised when runtime evidence or owned output cannot be verified."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: object, description: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise RuntimeTermsError(f"Unsafe {description}: {value!r}")
    if re.match(r"^[A-Za-z]:", value):
        raise RuntimeTermsError(f"Unsafe {description}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise RuntimeTermsError(f"Unsafe {description}: {value!r}")
    return path


def _contains_link(path: Path) -> bool:
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
        is_junction = getattr(current, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
    return False


def _regular_file(path: Path, *, description: str, limit: int) -> tuple[Path, bytes]:
    if _contains_link(path):
        raise RuntimeTermsError(f"{description} contains a symlink or unsafe path: {path}")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > limit:
            raise RuntimeTermsError(f"{description} is not a bounded, nonempty regular file: {path}")
        data = resolved.read_bytes()
    except OSError as exc:
        raise RuntimeTermsError(f"Could not read {description}: {path}: {exc}") from exc
    return resolved, data


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _runtime_roots(python_base: Path, venv_root: Path) -> tuple[Path, Path]:
    try:
        base = Path(python_base).resolve(strict=True)
        site_packages = (Path(venv_root) / "Lib" / "site-packages").resolve(strict=True)
    except OSError as exc:
        raise RuntimeTermsError(f"Python base or virtual-environment site-packages is unavailable: {exc}") from exc
    return base, site_packages


def _runtime_origin(source: Path, python_base: Path, site_packages: Path) -> str:
    for root, label in ((python_base, "CPython"), (site_packages, "venv/site-packages")):
        if _within(source, root):
            relative = source.relative_to(root).as_posix()
            _safe_relative(relative, "runtime source relative path")
            return f"{label}/{relative}"
    raise RuntimeTermsError(f"Microsoft runtime source is outside the approved Python roots: {source}")


def _load_evidence(path: Path) -> list[dict]:
    _resolved, data = _regular_file(path, description="native Windows evidence", limit=MAX_METADATA_BYTES)
    try:
        rows = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeTermsError(f"Native Windows evidence is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeTermsError("Native Windows evidence must be a list of records")
    return rows


def _read_template(path: Path) -> tuple[bytes, str]:
    if path.name != "MICROSOFT-RUNTIME-TERMS.txt":
        raise RuntimeTermsError("Expected the final MICROSOFT-RUNTIME-TERMS.txt template")
    _resolved, data = _regular_file(path, description="Microsoft runtime terms template", limit=MAX_TEMPLATE_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeTermsError(f"Microsoft runtime terms template is not UTF-8: {exc}") from exc
    if not text.strip() or TEMPLATE_PLACEHOLDER.search(text):
        raise RuntimeTermsError("Microsoft runtime terms template is empty or contains an unresolved placeholder")
    return data, text


def _collect_runtime_records(toc_rows, evidence_rows, python_base: Path, site_packages: Path) -> list[dict]:
    expected = {}
    for row in toc_rows:
        if not isinstance(row, (tuple, list)) or len(row) != 3 or not all(isinstance(value, str) for value in row):
            continue
        destination, source_text, kind = row
        basename = PurePosixPath(destination.replace("\\", "/")).name.casefold()
        if MSVC_LOOKING_DLL.fullmatch(basename) and basename not in MICROSOFT_RUNTIME_DLLS:
            raise RuntimeTermsError(f"Unrecognized retained Microsoft-runtime-looking DLL is not covered: {destination}")
        if basename not in MICROSOFT_RUNTIME_DLLS:
            continue
        relative_destination = _safe_relative(destination.replace("\\", "/"), "runtime bundle destination")
        source_path = Path(source_text)
        if not source_path.is_absolute() or _contains_link(source_path):
            raise RuntimeTermsError(f"Runtime source is not an absolute, nonsymlink path: {source_text}")
        resolved_source, data = _regular_file(source_path, description="runtime DLL source", limit=MAX_METADATA_BYTES * 4)
        origin = _runtime_origin(resolved_source, python_base, site_packages)
        digest = _sha256(data)
        identity = (relative_destination.as_posix(), str(resolved_source).casefold())
        if any(existing[0] == identity[0] and existing[1] != identity[1] for existing in expected):
            raise RuntimeTermsError(f"Distinct runtime sources map to the same bundle destination: {destination}")
        expected[identity] = {
            "destination": relative_destination.as_posix(), "source_path": resolved_source,
            "source": origin, "sha256": digest, "size_bytes": len(data),
        }

    evidence_by_identity = {}
    for entry in evidence_rows:
        destination = entry.get("destination")
        source = entry.get("source")
        if not isinstance(destination, str) or not isinstance(source, str):
            continue
        basename = PurePosixPath(destination.replace("\\", "/")).name.casefold()
        if basename not in MICROSOFT_RUNTIME_DLLS:
            continue
        rel_destination = _safe_relative(destination.replace("\\", "/"), "evidence destination").as_posix()
        source_path = Path(source)
        if not source_path.is_absolute() or _contains_link(source_path):
            raise RuntimeTermsError(f"Runtime evidence source is unsafe: {source}")
        try:
            resolved_source = source_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeTermsError(f"Runtime evidence source is unavailable: {source}") from exc
        identity = (rel_destination, str(resolved_source).casefold())
        if identity in evidence_by_identity:
            raise RuntimeTermsError(f"Duplicate runtime DLL evidence record: {destination}")
        evidence_by_identity[identity] = entry

    records = []
    for identity, expected_entry in sorted(expected.items()):
        entry = evidence_by_identity.get(identity)
        if entry is None:
            raise RuntimeTermsError(
                f"Missing exact native evidence for retained runtime DLL: {expected_entry['destination']}"
            )
        if entry.get("sha256") != expected_entry["sha256"]:
            raise RuntimeTermsError(f"Runtime DLL evidence hash does not match source: {expected_entry['destination']}")
        company = entry.get("company")
        version = entry.get("file_version")
        copyright = entry.get("legal_copyright")
        if not isinstance(company, str) or company.strip().casefold() != "microsoft corporation":
            raise RuntimeTermsError(f"Runtime DLL company is missing or is not Microsoft: {expected_entry['destination']}")
        if (not isinstance(version, str) or not version.strip() or len(version) > 256
                or any(ord(character) < 32 for character in version)):
            raise RuntimeTermsError(f"Runtime DLL version is missing: {expected_entry['destination']}")
        if not isinstance(copyright, str) or not copyright.strip():
            raise RuntimeTermsError(f"Runtime DLL legal copyright metadata is missing: {expected_entry['destination']}")
        if entry.get("is_debug") is not False or entry.get("is_pre_release") is not False:
            raise RuntimeTermsError(f"Runtime DLL is debug/pre-release or lacks release status: {expected_entry['destination']}")
        records.append({
            "destination": expected_entry["destination"], "source": expected_entry["source"],
            "sha256": expected_entry["sha256"], "size_bytes": expected_entry["size_bytes"],
            "version": version.strip(), "company": company.strip(), "legal_copyright": copyright,
            "is_debug": False, "is_pre_release": False,
        })
    if not records:
        raise RuntimeTermsError("No retained, supported Microsoft runtime DLLs were found")
    return records


def _render_terms(template_text: str, records: list[dict]) -> bytes:
    lines = ["## Covered Microsoft runtime files", "",
             "The following file identities and version-resource fields were observed in this build. "
             "They are provenance facts, not a separate grant or a determination of redistribution rights.", ""]

    def code_span(value: str) -> str:
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
        marker = "`" * (longest + 1)
        padding = " " if value.startswith("`") or value.endswith("`") else ""
        return f"{marker}{padding}{value}{padding}{marker}"

    for record in records:
        lines.extend([
            f"### {code_span(record['destination'])}", "",
            f"- Version: {code_span(record['version'])}",
            f"- SHA-256: {code_span(record['sha256'])}",
            f"- Upstream-relative origin: {code_span(record['source'])}",
            f"- Version-resource company: {code_span(record['company'])}",
            "- Version-resource legal copyright (verbatim):", "",
        ])
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", record["legal_copyright"])), default=0)
        fence = "`" * max(3, longest + 1)
        lines.extend([f"{fence}text", record["legal_copyright"], fence, ""])
    appended = ("\n".join(lines) + "\n").encode("utf-8")
    separator = b"" if template_text.endswith(("\n", "\r")) else b"\n"
    return template_text.encode("utf-8") + separator + b"\n" + appended


def _output_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink() or (getattr(current, "is_junction", None) and current.is_junction()):
            raise RuntimeTermsError(f"Runtime terms output traverses a symlink or junction: {current}")
    absolute.mkdir(parents=True, exist_ok=True)
    if absolute.is_symlink() or not absolute.is_dir():
        raise RuntimeTermsError("Runtime terms output is not a real directory")
    return absolute.resolve(strict=True)


def _validate_existing_output(output_root: Path) -> set[str]:
    manifest_path = output_root / "manifest.json"
    terms_path = output_root / "TERMS.txt"
    if manifest_path.is_symlink() or terms_path.is_symlink():
        raise RuntimeTermsError("Existing runtime terms output contains a symlink")
    if not manifest_path.exists():
        if terms_path.exists():
            raise RuntimeTermsError("Refusing to overwrite an unowned runtime terms file")
        return set()
    try:
        if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_METADATA_BYTES:
            raise RuntimeTermsError("Existing runtime terms manifest is not a bounded regular file")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeTermsError("Existing runtime terms manifest is corrupt") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("owner") != "nova-mic-switch-windows-runtime-terms"):
        raise RuntimeTermsError("Existing runtime terms manifest has an unsupported schema")
    owned = manifest.get("owned_files")
    if not isinstance(owned, list):
        raise RuntimeTermsError("Existing runtime terms manifest lacks an ownership inventory")
    owned_paths = set()
    for item in owned:
        if not isinstance(item, dict) or item.get("path") != "TERMS.txt" or "TERMS.txt" in owned_paths:
            raise RuntimeTermsError("Existing runtime terms manifest has invalid ownership records")
        expected_hash, expected_size = item.get("sha256"), item.get("size_bytes")
        if not isinstance(expected_hash, str) or type(expected_size) is not int:
            raise RuntimeTermsError("Existing runtime terms ownership record is incomplete")
        _resolved, data = _regular_file(terms_path, description="owned runtime terms", limit=MAX_TEMPLATE_BYTES * 2)
        if len(data) != expected_size or _sha256(data) != expected_hash:
            raise RuntimeTermsError("Previously generated runtime terms are missing or tampered")
        owned_paths.add("TERMS.txt")
    if "TERMS.txt" not in owned_paths:
        raise RuntimeTermsError("Existing runtime terms manifest does not own TERMS.txt")
    return owned_paths


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".runtime-terms-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def collect_microsoft_runtime_terms(*, toc_rows, evidence_path: Path, template_path: Path,
                                    output_dir: Path, python_base: Path, venv_root: Path) -> dict:
    """Create owned terms text from exact retained-file and Windows version metadata."""
    evidence_rows = _load_evidence(Path(evidence_path))
    template_data, template_text = _read_template(Path(template_path))
    python_root, site_packages = _runtime_roots(Path(python_base), Path(venv_root))
    records = _collect_runtime_records(toc_rows, evidence_rows, python_root, site_packages)
    terms_data = _render_terms(template_text, records)
    output_root = _output_root(Path(output_dir))
    _validate_existing_output(output_root)
    terms_record = {
        "path": "licenses/windows-runtime/TERMS.txt",
        "sha256": _sha256(terms_data),
        "size_bytes": len(terms_data),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "owner": "nova-mic-switch-windows-runtime-terms",
        "basis": "Ordinary unmodified upstream CPython/PySide application-distributor route; version-resource observations are facts, not a standalone license grant or legal determination.",
        "template": {
            "source": "packaging/windows/MICROSOFT-RUNTIME-TERMS.txt",
            "sha256": _sha256(template_data), "size_bytes": len(template_data),
        },
        "terms": terms_record,
        "runtime_files": records,
        "owned_files": [{**terms_record, "path": "TERMS.txt"}],
    }
    manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    terms_path = output_root / "TERMS.txt"
    manifest_path = output_root / "manifest.json"
    for path in (terms_path, manifest_path):
        if path.is_symlink():
            raise RuntimeTermsError(f"Runtime terms output is a symlink: {path}")
    _atomic_write(terms_path, terms_data)
    _atomic_write(manifest_path, manifest_data)
    return {"manifest": str(manifest_path), "files": [str(manifest_path), str(terms_path)]}


def verify_runtime_terms(*, bundle: Path, manifest_path: Path) -> dict:
    """Verify bundled runtime copies are unchanged from the captured source evidence."""
    bundle_path = Path(bundle)
    if _contains_link(bundle_path) or not bundle_path.is_dir():
        raise RuntimeTermsError("Windows bundle root is not a real directory")
    resolved_bundle = bundle_path.resolve(strict=True)
    internal_root = resolved_bundle / "_internal"
    manifest_file = Path(manifest_path)
    _manifest_resolved, manifest_bytes = _regular_file(
        manifest_file, description="generated runtime manifest", limit=MAX_METADATA_BYTES,
    )
    bundled_manifest_file = internal_root / "licenses" / "windows-runtime" / "manifest.json"
    bundled_manifest_resolved, bundled_manifest_bytes = _regular_file(
        bundled_manifest_file, description="bundled runtime manifest", limit=MAX_METADATA_BYTES,
    )
    try:
        manifest_rel = bundled_manifest_resolved.relative_to(internal_root)
    except ValueError as exc:
        raise RuntimeTermsError("Bundled runtime manifest is outside the application _internal directory") from exc
    if manifest_rel.as_posix() != "licenses/windows-runtime/manifest.json":
        raise RuntimeTermsError("Bundled runtime manifest is at an unexpected path")
    if bundled_manifest_bytes != manifest_bytes:
        raise RuntimeTermsError("Bundled runtime manifest differs from the generated manifest")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeTermsError(f"Bundled runtime manifest is invalid JSON: {exc}") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("owner") != "nova-mic-switch-windows-runtime-terms"):
        raise RuntimeTermsError("Bundled runtime manifest has an unsupported schema")
    records = manifest.get("runtime_files")
    terms = manifest.get("terms")
    if not isinstance(records, list) or not records or not isinstance(terms, dict):
        raise RuntimeTermsError("Bundled runtime manifest lacks runtime files or terms inventory")
    to_check = list(records) + [terms]
    seen = set()
    for entry in to_check:
        if not isinstance(entry, dict):
            raise RuntimeTermsError("Bundled runtime manifest contains an invalid file record")
        relative = _safe_relative(entry.get("destination", entry.get("path")), "bundle destination")
        if relative.as_posix() in seen:
            raise RuntimeTermsError(f"Duplicate bundled runtime destination: {relative}")
        seen.add(relative.as_posix())
        size, expected_hash = entry.get("size_bytes"), entry.get("sha256")
        if type(size) is not int or size <= 0 or not isinstance(expected_hash, str):
            raise RuntimeTermsError(f"Bundled runtime manifest has invalid file metadata: {relative}")
        target = internal_root.joinpath(*relative.parts)
        try:
            resolved, data = _regular_file(target, description="bundled runtime file", limit=MAX_METADATA_BYTES * 4)
            resolved.relative_to(internal_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise RuntimeTermsError(f"Bundled runtime file is missing or escapes _internal: {relative}") from exc
        if len(data) != size or _sha256(data) != expected_hash:
            raise RuntimeTermsError(f"Bundled runtime file was modified or has the wrong size: {relative}")
    if "licenses/windows-runtime/TERMS.txt" not in seen:
        raise RuntimeTermsError("Bundled runtime terms file is missing from its manifest")
    return {"verified_files": len(to_check) + 1, "runtime_files": len(records)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify bundled runtime terms and unchanged DLLs")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_runtime_terms(bundle=args.bundle, manifest_path=args.manifest)
    except RuntimeTermsError as exc:
        parser.error(str(exc))
    print(f"Verified {result['runtime_files']} Microsoft runtime DLLs, terms text, and bundled manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
