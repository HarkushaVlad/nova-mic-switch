#!/usr/bin/env python3
"""Fetch the three pinned AppImage runtime source archives into a local cache."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import tempfile
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging/licenses/appimage-runtime/manifest.json"
ALLOWED_HOSTS = frozenset({
    "github.com", "codeload.github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
})
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
TIMEOUT_SECONDS = 45


class RuntimeSourceError(RuntimeError):
    """Pinned runtime source material is invalid or unavailable."""


def _url(url: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RuntimeSourceError(f"Invalid runtime source URL: {url!r}") from exc
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment
            or "\\" in parsed.path or any(ord(ch) < 32 or ord(ch) == 127 for ch in parsed.path)
            or any(part == ".." for part in urllib.parse.unquote(parsed.path).split("/"))):
        raise RuntimeSourceError(f"Runtime source URL is outside the HTTPS allowlist: {url}")
    return parsed


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _records(manifest_path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeSourceError(f"Could not read runtime source manifest: {exc}") from exc
    rows = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 3:
        raise RuntimeSourceError("Runtime manifest must contain exactly three pinned source archives")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeSourceError("Invalid runtime source record")
        filename, url, digest, size = (row.get(k) for k in ("filename", "url", "archive_sha256", "archive_bytes"))
        if (not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+", filename)
                or filename in seen or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or size <= 0 or size > MAX_ARCHIVE_BYTES):
            raise RuntimeSourceError("Runtime source record has an unsafe filename, size, or SHA-256 pin")
        parsed = _url(url)
        if parsed.hostname not in {"github.com", "codeload.github.com"} or not parsed.path.startswith("/") or ".." in parsed.path.split("/") or parsed.query:
            raise RuntimeSourceError(f"Runtime source URL has an unapproved host or path: {url}")
        archive_path = row.get("archive_path")
        if archive_path != f"build/runtime-materials/sources/{filename}":
            raise RuntimeSourceError(f"Unexpected runtime archive destination: {archive_path!r}")
        seen.add(filename)
        result.append({"filename": filename, "url": url, "sha256": digest, "size": size})
    return result


def _hash(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def fetch_sources(output_dir: Path, *, fetch: bool = False, manifest_path: Path = MANIFEST) -> list[Path]:
    records = _records(manifest_path)
    absolute = Path(os.path.abspath(output_dir))
    for component in (*reversed(absolute.parents), absolute):
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if component.is_symlink() or not component.is_dir():
                raise RuntimeSourceError(f"Unsafe runtime output directory component: {component}")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for row in records:
        target = output_dir / row["filename"]
        if target.is_symlink():
            raise RuntimeSourceError(f"Cached runtime source is a symlink: {target}")
        if target.exists():
            if not stat.S_ISREG(target.lstat().st_mode):
                raise RuntimeSourceError(f"Cached runtime source is not a regular file: {target}")
            size, digest = _hash(target)
            if size != row["size"] or digest != row["sha256"]:
                raise RuntimeSourceError(f"Cached runtime source failed size/SHA-256 verification: {target.name}")
            results.append(target)
            continue
        if not fetch:
            raise RuntimeSourceError(f"Offline cache miss for {target.name}; pass --fetch to allow HTTPS downloads")
        request = urllib.request.Request(row["url"], headers={"User-Agent": "NovaMicSwitch-runtime-sources/1", "Accept-Encoding": "identity"})
        temporary = None
        try:
            _url(request.full_url)
            opener = urllib.request.build_opener(_Redirects())
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                _url(response.geturl())
                if response.status != 200:
                    raise RuntimeSourceError(f"HTTP {response.status} downloading {target.name}")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) != row["size"] or int(length) > MAX_ARCHIVE_BYTES):
                    raise RuntimeSourceError(f"Invalid Content-Length for {target.name}")
                fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=output_dir)
                temporary = Path(temporary_name)
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as out:
                    while block := response.read(128 * 1024):
                        size += len(block)
                        if size > row["size"] or size > MAX_ARCHIVE_BYTES:
                            raise RuntimeSourceError(f"Runtime source exceeds its pinned size: {target.name}")
                        digest.update(block)
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                if size != row["size"] or digest.hexdigest() != row["sha256"]:
                    raise RuntimeSourceError(f"Downloaded runtime source failed size/SHA-256 verification: {target.name}")
            if target.exists() or target.is_symlink():
                raise RuntimeSourceError(f"Runtime source appeared during fetch: {target}")
            os.replace(temporary, target)
            temporary = None
            results.append(target)
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            raise RuntimeSourceError(f"Could not fetch {target.name}: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/runtime-materials/sources")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--fetch", action="store_true", help="explicitly allow HTTPS downloads for cache misses")
    args = parser.parse_args(argv)
    try:
        for path in fetch_sources(args.output_dir, fetch=args.fetch, manifest_path=args.manifest):
            size, digest = _hash(path)
            print(f"{path.name}: {size} bytes sha256:{digest}")
    except RuntimeSourceError as exc:
        print(f"runtime source error: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
