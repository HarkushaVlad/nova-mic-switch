#!/usr/bin/env python3
"""Capture exact Ubuntu source packages referenced by a native evidence report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.parser import Parser
from pathlib import Path
from typing import Any, Iterable


API_ROOT = "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
ALLOWED_HOSTS = {"api.launchpad.net", "launchpad.net", "launchpadlibrarian.net"}
MAX_METADATA = 2 * 1024 * 1024
MAX_DSC = 2 * 1024 * 1024
MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_PAGES = 50
RETRIES = 3
TIMEOUT = 45


class SourceError(RuntimeError):
    """Raised when source metadata or cached material cannot be trusted."""


def cache_key(source: str, version: str) -> str:
    return hashlib.sha256((source + "\0" + version).encode("utf-8")).hexdigest()


def source_pairs(report: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for entry in report.get("native_files", []):
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        if not isinstance(provider, dict) or provider.get("kind") != "system-package":
            continue
        name, version = entry.get("source_package"), entry.get("source_version")
        if isinstance(name, str) and name and isinstance(version, str) and version:
            pairs.add((name, version))
    return sorted(pairs)


def _validate_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise SourceError(f"Rejected non-official HTTPS URL: {url}")
    return parsed


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request:
        old = _validate_url(req.full_url)
        new = _validate_url(newurl)
        if new.hostname not in ALLOWED_HOSTS:
            raise SourceError(f"Rejected redirect target: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(OfficialRedirect())


def _request_bytes(url: str, *, max_bytes: int, fetch: bool) -> tuple[bytes, int]:
    if not fetch:
        raise SourceError("Network access is disabled; pass --fetch to retrieve uncached sources")
    _validate_url(url)
    opener = _opener()
    last_error: BaseException | None = None
    for attempt in range(RETRIES):
        request = urllib.request.Request(url, headers={"User-Agent": "ubuntu-source-capture/1.0", "Accept-Encoding": "identity"})
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise SourceError(f"HTTP {response.status} for {url}")
                _validate_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not length.isdigit() or int(length) > max_bytes:
                        raise SourceError(f"Invalid or oversized Content-Length for {url}: {length}")
                chunks: list[bytes] = []
                total = 0
                while True:
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise SourceError(f"Response exceeds {max_bytes} bytes: {url}")
                    chunks.append(block)
                if length is not None and total != int(length):
                    raise SourceError(f"Content-Length mismatch for {url}: received {total}, expected {length}")
                return b"".join(chunks), response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code <= 599:
                last_error = exc
            else:
                raise SourceError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
        if attempt + 1 < RETRIES:
            time.sleep(0.2 * (attempt + 1))
    raise SourceError(f"Transient request failed after {RETRIES} attempts: {url}: {last_error}")


def _request_json(url: str, *, fetch: bool) -> tuple[Any, int]:
    data, status = _request_bytes(url, max_bytes=MAX_METADATA, fetch=fetch)
    try:
        return json.loads(data.decode("utf-8")), status
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"Invalid Launchpad JSON from {url}") from exc


def _api_url(source: str, version: str, status: str) -> str:
    query = urllib.parse.urlencode({
        "ws.op": "getPublishedSources",
        "source_name": source,
        "version": version,
        "exact_match": "true",
        "status": status,
        "ws.size": "75",
    })
    return API_ROOT + "?" + query


def _series_is_jammy(link: Any) -> bool:
    if not isinstance(link, str):
        return False
    parsed = urllib.parse.urlparse(link)
    return parsed.scheme == "https" and parsed.hostname == "api.launchpad.net" and parsed.path.rstrip("/").endswith("/ubuntu/jammy")


def published_source_entries(source: str, version: str, *, fetch: bool) -> tuple[list[dict[str, Any]], str]:
    """Find exact Jammy source publications; only use Superseded as explicit fallback."""
    for state in ("Published", "Superseded"):
        url: str | None = _api_url(source, version, state)
        seen_pages: set[str] = set()
        entries: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            if not url or url in seen_pages:
                break
            _validate_url(url)
            seen_pages.add(url)
            payload, _ = _request_json(url, fetch=fetch)
            if not isinstance(payload, dict) or not isinstance(payload.get("entries", []), list):
                raise SourceError(f"Unexpected Launchpad publication response for {source}={version}")
            for row in payload.get("entries", []):
                if not isinstance(row, dict):
                    continue
                # Launchpad currently omits source_name from some publication
                # records even when the exact source_name query is applied.
                # If it is supplied, reject a conflicting value; exact_match
                # and the returned exact version remain mandatory.
                if row.get("source_name") not in (None, source) or row.get("source_package_version") != version:
                    continue
                if not _series_is_jammy(row.get("distro_series_link")):
                    continue
                self_link = row.get("self_link")
                if isinstance(self_link, str):
                    _validate_url(self_link)
                    entries.append(row)
            next_url = payload.get("next_collection_link")
            if next_url is not None and not isinstance(next_url, str):
                raise SourceError("Invalid Launchpad pagination URL")
            url = next_url
        else:
            raise SourceError(f"Launchpad pagination exceeds {MAX_PAGES} pages for {source}={version}")
        if entries:
            return entries, state
    raise SourceError(f"No exact Published or Superseded Jammy source publication: {source}={version}")


def source_file_urls(entry: dict[str, Any], *, fetch: bool) -> list[str]:
    link = entry["self_link"]
    parsed = urllib.parse.urlparse(link)
    url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode({"ws.op": "sourceFileUrls"}), ""))
    payload, _ = _request_json(url, fetch=fetch)
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise SourceError(f"Unexpected sourceFileUrls response for {link}")
    urls: list[str] = []
    for item in payload:
        _validate_url(item)
        urls.append(item)
    return urls


def _safe_basename(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name and not any(ord(c) < 32 or ord(c) == 127 for c in name)


def parse_dsc(data: bytes, expected_source: str, expected_version: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(".dsc is not UTF-8 Deb822") from exc
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        raise SourceError(".dsc is not a clear-signed Deb822 envelope")
    if text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        lines = text.splitlines()
        try:
            body_start = lines.index("") + 1
            signature = next(i for i, line in enumerate(lines[body_start:], body_start) if line.startswith("-----BEGIN PGP SIGNATURE-----"))
        except (ValueError, StopIteration) as exc:
            raise SourceError("Malformed clear-signed .dsc envelope") from exc
        text = "\n".join(line[2:] if line.startswith("- ") else line for line in lines[body_start:signature]) + "\n"
    try:
        fields = Parser().parsestr(text, headersonly=True)
    except Exception as exc:
        raise SourceError("Unable to parse .dsc Deb822 fields") from exc
    source = fields.get("Source")
    version = fields.get("Version")
    if source != expected_source or version != expected_version:
        raise SourceError(f".dsc identity mismatch: expected {expected_source}={expected_version}, got {source}={version}")
    checksum = fields.get("Checksums-Sha256")
    if not checksum:
        raise SourceError(".dsc lacks Checksums-Sha256")
    records: list[dict[str, Any]] = []
    for line in checksum.splitlines():
        parts = line.split()
        if len(parts) != 3:
            raise SourceError(f"Malformed Checksums-Sha256 row: {line!r}")
        digest, size_text, filename = parts
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest) or not size_text.isdigit() or not _safe_basename(filename):
            raise SourceError(f"Unsafe or malformed Checksums-Sha256 row: {line!r}")
        records.append({"filename": filename, "size_bytes": int(size_text), "sha256": digest.lower()})
    if not records or len({r["filename"] for r in records}) != len(records):
        raise SourceError(".dsc has no archive checksums or duplicate filenames")
    return records


def _atomic_write(path: Path, data: bytes, allowed_root: Path) -> None:
    root = allowed_root.resolve()
    if path.is_symlink():
        raise SourceError(f"Refusing symlink output: {path}")
    parent = path.parent
    if not parent.resolve().is_relative_to(root):
        raise SourceError(f"Output escapes allowed root: {path}")
    parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not parent.resolve().is_relative_to(root):
        raise SourceError(f"Output path became unsafe: {path}")
    fd, tmp = tempfile.mkstemp(prefix=".ubuntu-source-", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_cached(cache_dir: Path, source: str, version: str) -> dict[str, Any]:
    key = cache_key(source, version)
    package_dir = cache_dir / key
    if package_dir.is_symlink() or not package_dir.resolve().is_relative_to(cache_dir.resolve()):
        raise SourceError(f"Unsafe cache package directory for {source}={version}")
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SourceError(f"No valid offline cache entry for {source}={version}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SourceError(f"Invalid cache manifest for {source}={version}") from exc
    if manifest.get("source_package") != source or manifest.get("source_version") != version or manifest.get("cache_key") != key:
        raise SourceError(f"Cache key metadata mismatch for {source}={version}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SourceError(f"Cache has no file records for {source}={version}")
    records_by_name: dict[str, dict[str, Any]] = {}
    for record in files:
        if not isinstance(record, dict):
            raise SourceError(f"Malformed file record in cache for {source}={version}")
        name = record.get("filename")
        if not isinstance(name, str) or not _safe_basename(name):
            raise SourceError(f"Unsafe filename in cache for {source}={version}")
        if name in records_by_name:
            raise SourceError(f"Duplicate filename in cache manifest: {name}")
        urls = record.get("urls")
        if not isinstance(urls, list) or not urls:
            raise SourceError(f"Missing source URLs in cache manifest: {name}")
        for url in urls:
            _validate_url(url)
        path = package_dir / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(package_dir.resolve()):
            raise SourceError(f"Missing or unsafe cached file: {path}")
        data = path.read_bytes()
        if len(data) != record.get("size_bytes") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
            raise SourceError(f"Cached file integrity mismatch: {path}")
        records_by_name[name] = record
    dsc_records = [r for r in files if r.get("digest_source", "").startswith("official HTTPS .dsc")]
    if len(dsc_records) != 1:
        raise SourceError(f"Cache must contain exactly one official .dsc record for {source}={version}")
    dsc_record = dsc_records[0]
    if dsc_record.get("sha256") != manifest.get("dsc_sha256"):
        raise SourceError(f"Cached .dsc pin mismatch for {source}={version}")
    dsc_data = (package_dir / dsc_record["filename"]).read_bytes()
    check_records = parse_dsc(dsc_data, source, version)
    if {r["filename"] for r in check_records} != set(records_by_name) - {dsc_record["filename"]}:
        raise SourceError(f"Cached file set does not match its .dsc for {source}={version}")
    for expected in check_records:
        cached = records_by_name[expected["filename"]]
        if cached.get("sha256") != expected["sha256"] or cached.get("size_bytes") != expected["size_bytes"]:
            raise SourceError(f"Cached archive metadata does not match its .dsc: {expected['filename']}")
    return manifest


def _download_file(url: str, max_bytes: int, fetch: bool) -> bytes:
    data, _ = _request_bytes(url, max_bytes=max_bytes, fetch=fetch)
    return data


def _download_to_file(url: str, target: Path, *, max_bytes: int, fetch: bool) -> tuple[int, str]:
    """Stream one bounded response to an owned temporary file, then atomically publish it."""
    if not fetch:
        raise SourceError("Network access is disabled; pass --fetch to retrieve uncached sources")
    _validate_url(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise SourceError(f"Refusing symlink download target: {target}")
    opener = _opener()
    last_error: BaseException | None = None
    for attempt in range(RETRIES):
        temp_name: str | None = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "ubuntu-source-capture/1.0", "Accept-Encoding": "identity"})
            with opener.open(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise SourceError(f"HTTP {response.status} for {url}")
                _validate_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > max_bytes):
                    raise SourceError(f"Invalid or oversized Content-Length for {url}: {length}")
                fd, temp_name = tempfile.mkstemp(prefix=".ubuntu-download-", dir=str(target.parent))
                digest = hashlib.sha256()
                total = 0
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        block = response.read(128 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise SourceError(f"Response exceeds {max_bytes} bytes: {url}")
                        stream.write(block)
                        digest.update(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                if length is not None and total != int(length):
                    raise SourceError(f"Content-Length mismatch for {url}: received {total}, expected {length}")
                if target.is_symlink():
                    raise SourceError(f"Refusing symlink download target: {target}")
                os.replace(temp_name, target)
                temp_name = None
                return total, digest.hexdigest()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code <= 599:
                last_error = exc
            else:
                raise SourceError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
        if attempt + 1 < RETRIES:
            time.sleep(0.2 * (attempt + 1))
    raise SourceError(f"Transient download failed after {RETRIES} attempts: {url}: {last_error}")


def fetch_source(cache_dir: Path, source: str, version: str, *, fetch: bool) -> dict[str, Any]:
    if not fetch:
        return _read_cached(cache_dir, source, version)
    key = cache_key(source, version)
    target = cache_dir / key
    if target.exists():
        # A corrupt existing cache must not be silently repinned by --fetch.
        return _read_cached(cache_dir, source, version)
    publications, state = published_source_entries(source, version, fetch=True)
    publication_urls: list[tuple[str, list[str]]] = []
    for publication in publications:
        urls = source_file_urls(publication, fetch=True)
        dscs = [u for u in urls if urllib.parse.urlparse(u).path.rsplit("/", 1)[-1].endswith(".dsc")]
        if len(dscs) != 1:
            raise SourceError(f"Expected one .dsc URL in publication {publication['self_link']}")
        publication_urls.append((dscs[0], urls))

    downloaded_dscs: list[tuple[bytes, str, list[str]]] = []
    for dsc_url, urls in publication_urls:
        dsc_data = _download_file(dsc_url, MAX_DSC, True)
        downloaded_dscs.append((dsc_data, dsc_url, urls))
    first_dsc = downloaded_dscs[0][0]
    dsc_sha = hashlib.sha256(first_dsc).hexdigest()
    if any(hashlib.sha256(data).hexdigest() != dsc_sha for data, _, _ in downloaded_dscs[1:]):
        raise SourceError(f"Duplicate publication .dsc bytes differ for {source}={version}")
    checksums = parse_dsc(first_dsc, source, version)
    urls_by_name: dict[str, list[str]] = {}
    for _, _, urls in downloaded_dscs:
        for url in urls:
            basename = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
            if not _safe_basename(basename):
                raise SourceError(f"Unsafe file URL basename: {url}")
            urls_by_name.setdefault(basename, []).append(url)
    dsc_name = urllib.parse.unquote(urllib.parse.urlparse(downloaded_dscs[0][1]).path.rsplit("/", 1)[-1])
    required = {r["filename"] for r in checksums}
    if not required.issubset(urls_by_name):
        missing = sorted(required - urls_by_name.keys())
        raise SourceError(f".dsc references files absent from official sourceFileUrls: {missing}")
    if dsc_name not in urls_by_name:
        raise SourceError(".dsc URL absent from its official sourceFileUrls response")

    cache_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir.is_symlink():
        raise SourceError(f"Cache root is a symlink: {cache_dir}")
    stage = Path(tempfile.mkdtemp(prefix=".ubuntu-source-stage-", dir=str(cache_dir)))
    records: list[dict[str, Any]] = []
    try:
        _atomic_write(stage / dsc_name, first_dsc, stage)
        records.append({"filename": dsc_name, "size_bytes": len(first_dsc), "sha256": dsc_sha, "urls": sorted(set(u for _, u, _ in downloaded_dscs)),
                        "digest_source": "official HTTPS .dsc response; no upstream checksum asserted for the .dsc"})
        checksum_by_name = {r["filename"]: r for r in checksums}
        for filename in sorted(required):
            expected = checksum_by_name[filename]
            if expected["size_bytes"] > MAX_ARCHIVE:
                raise SourceError(f"Archive exceeds {MAX_ARCHIVE} bytes: {filename}")
            urls = sorted(set(urls_by_name[filename]))
            target_file = stage / filename
            actual_size, actual_sha = _download_to_file(urls[0], target_file, max_bytes=MAX_ARCHIVE, fetch=True)
            if actual_size != expected["size_bytes"] or actual_sha != expected["sha256"]:
                raise SourceError(f".dsc checksum/size mismatch for {filename}")
            for duplicate_number, duplicate_url in enumerate(urls[1:], 1):
                duplicate_path = stage / f".duplicate-{duplicate_number}-{filename}"
                try:
                    duplicate_size, duplicate_sha = _download_to_file(duplicate_url, duplicate_path, max_bytes=MAX_ARCHIVE, fetch=True)
                    if duplicate_size != actual_size or duplicate_sha != actual_sha:
                        raise SourceError(f"Duplicate publication file bytes differ: {filename}")
                finally:
                    if duplicate_path.exists():
                        duplicate_path.unlink()
            records.append({"filename": filename, "size_bytes": actual_size, "sha256": actual_sha, "urls": urls,
                            "digest_source": "Checksums-Sha256 in the captured .dsc"})
        if len({r["filename"] for r in records}) != len(records):
            raise SourceError("Duplicate filename collision in captured source set")
        manifest = {
            "schema_version": 1,
            "cache_key": key,
            "source_package": source,
            "source_version": version,
            "distribution_series": "jammy",
            "publication_status": state,
            "publication_links": sorted({row["self_link"] for row in publications}),
            "dsc_sha256": dsc_sha,
            "dsc_signature": "clear-signed envelope retained; signature not cryptographically verified; trust basis is official HTTPS and .dsc checksums",
            "files": sorted(records, key=lambda row: row["filename"]),
        }
        _atomic_write(stage / "manifest.json", (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(), stage)
        os.replace(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return _read_cached(cache_dir, source, version)


def build_output(report_path: Path, cache_dir: Path, output_dir: Path, *, fetch: bool,
                 selected_packages: set[str] | None = None) -> dict[str, Any]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SourceError(f"Cannot read report JSON: {report_path}") from exc
    pairs = source_pairs(report)
    if not pairs:
        raise SourceError("Report contains no system-package source_package/source_version pairs")
    selected = [pair for pair in pairs if selected_packages is None or pair[0] in selected_packages]
    if selected_packages is not None:
        missing_names = selected_packages - {name for name, _ in pairs}
        if missing_names:
            raise SourceError(f"Requested package names absent from report: {sorted(missing_names)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink():
        raise SourceError(f"Output root is a symlink: {output_dir}")
    known = []
    sums: dict[str, str] = {}
    global_names: dict[str, str] = {}
    for source, version in selected:
        manifest = fetch_source(cache_dir, source, version, fetch=fetch)
        key = cache_key(source, version)
        source_dir = output_dir / "sources" / key
        for record in manifest["files"]:
            name = record["filename"]
            cache_path = cache_dir / key / name
            data = cache_path.read_bytes()
            relative = Path("sources") / key / name
            previous = global_names.get(name)
            if previous is not None and previous != record["sha256"]:
                raise SourceError(f"Duplicate source filename has different bytes: {name}")
            global_names[name] = record["sha256"]
            target = output_dir / relative
            if target.exists():
                if target.is_symlink() or target.read_bytes() != data:
                    raise SourceError(f"Existing output conflicts with captured bytes: {target}")
            else:
                _atomic_write(target, data, output_dir)
            sums[relative.as_posix()] = record["sha256"]
        known.append({"source_package": source, "source_version": version, "cache_key": key,
                      "publication_status": manifest["publication_status"],
                      "publication_links": manifest["publication_links"],
                      "files": [{"path": (Path("sources") / key / row["filename"]).as_posix(),
                                 "filename": row["filename"], "size_bytes": row["size_bytes"],
                                 "sha256": row["sha256"], "urls": row["urls"],
                                 "digest_source": row["digest_source"]} for row in manifest["files"]]})
    index = {
        "schema_version": 1,
        "scope": "Ubuntu Jammy source archives for exact report source_package/source_version pairs; unresolved native rows are not source-closed by this index.",
        "coverage": {"report_system_source_pairs": len(pairs), "captured_pairs_this_run": len(known),
                     "not_captured_pairs_this_run": len(pairs) - len(known),
                     "unresolved_native_count": len(report.get("unresolved", [])) if isinstance(report.get("unresolved", []), list) else None},
        "sources": known,
    }
    _atomic_write(output_dir / "ubuntu-source-index.json", (json.dumps(index, indent=2, ensure_ascii=False) + "\n").encode(), output_dir)
    lines = [f"{digest}  {path}" for path, digest in sorted(sums.items())]
    _atomic_write(output_dir / "UBUNTU-SHA256SUMS", ("\n".join(lines) + "\n").encode(), output_dir)
    markdown = ["# Ubuntu source materials", "", "Exact source pairs captured from the supplied native evidence report.", "",
                f"- Known system source pairs: {len(pairs)}", f"- Captured this run: {len(known)}",
                f"- Unresolved native rows: {index['coverage']['unresolved_native_count']}",
                "- This index does not claim source closure while unresolved native rows remain.", "", "## Sources", ""]
    for source in known:
        markdown.append(f"- `{source['source_package']}={source['source_version']}` ({source['publication_status']}; `{source['cache_key']}`)")
        for row in source["files"]:
            markdown.append(f"  - `{row['path']}` — {row['size_bytes']} bytes, SHA-256 `{row['sha256']}`")
    _atomic_write(output_dir / "UBUNTU-SOURCE-INDEX.md", ("\n".join(markdown) + "\n").encode(), output_dir)
    return index


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-json", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("build/ubuntu-source-cache"))
    parser.add_argument("--output-dir", type=Path, default=Path("dist/ubuntu-sources"))
    parser.add_argument("--fetch", action="store_true", help="Enable official HTTPS requests; default is strict offline")
    parser.add_argument("--package", action="append", dest="packages", help="Capture only this source package name (repeatable)")
    args = parser.parse_args(argv)
    try:
        result = build_output(args.report_json, args.cache_dir, args.output_dir, fetch=args.fetch,
                              selected_packages=set(args.packages) if args.packages else None)
    except SourceError as exc:
        print(f"ubuntu source capture failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result["coverage"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
