#!/usr/bin/env python3
"""Stage pinned upstream source archives and source-material provenance."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "packaging" / "sources" / "upstream.json"
MAX_ASSET_BYTES = 1024 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_RECIPE_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 45
ALLOWED_HOSTS = frozenset({
    "download.qt.io", "github.com", "codeload.github.com", "ffmpeg.org",
    "api.github.com", "raw.githubusercontent.com",
})
QT_CANONICAL_URL = "https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz"
QT_APPROVED_MIRROR_URL = "https://mirror.accum.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz"
QT_APPROVED_BACKEND_URL = "https://chuangtzu.ftp.acc.umu.se/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
EXPECTED_ASSETS = {
    "qt-everywhere-src": {
        "filename": "qt-everywhere-src-6.12.0.tar.xz",
        "version": "6.12.0",
        "url": "https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz",
        "sha256": "98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165",
        "size_bytes": 443394020,
    },
    "qt5-supermodule-source": {
        "filename": "qt5-ef25002ed292b132d1ed3246bfcee1d24a9cd966.tar.gz",
        "version": "6.12.0",
        "source_commit": "ef25002ed292b132d1ed3246bfcee1d24a9cd966",
        "url": "https://github.com/qt/qt5/archive/ef25002ed292b132d1ed3246bfcee1d24a9cd966.tar.gz",
    },
    "pyside-setup": {
        "filename": "pyside-setup-f50facb83fc3dc5f3240c854668c7a6dd93871b2.tar.gz",
        "version": "6.12.0",
        "source_commit": "f50facb83fc3dc5f3240c854668c7a6dd93871b2",
        "url": "https://github.com/pyside/pyside-setup/archive/f50facb83fc3dc5f3240c854668c7a6dd93871b2.tar.gz",
    },
    "ffmpeg": {
        "filename": "ffmpeg-9.0.1.tar.gz",
        "version": "9.0.1",
        "source_commit": "bf1b838f2ab88b4f8fd83443325c782ea0e0f7fa",
        "url": "https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.gz",
        "sha1": "89c318905212e6b67e10e42ab10ab8007e0aefda",
    },
}
RECIPE_PATHS = (
    "coin/provisioning/common/unix/ffmpeg-installation-utils.sh",
    "coin/provisioning/common/windows/install-ffmpeg.ps1",
    "coin/provisioning/common/shared/ffmpeg_config_options.txt",
    "coin/provisioning/common/shared/fix_ffmpeg_dependencies.sh",
)
QT_TAG_URL = "https://api.github.com/repos/qt/qt5/git/ref/tags/v6.12.0"
QT_RAW_URL = "https://raw.githubusercontent.com/qt/qt5/{commit}/{path}"


class SourceAssetError(RuntimeError):
    """Invalid source pin, source download, or cache/output error."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_https_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SourceAssetError(f"Invalid source URL: {url!r}") from exc
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or parsed.fragment):
        raise SourceAssetError(f"Source URL is outside the approved HTTPS host allowlist: {url}")


def _validate_redirect_target(source_url: str, target_url: str) -> None:
    if source_url == QT_CANONICAL_URL:
        if target_url == source_url:
            return
        if target_url == QT_APPROVED_MIRROR_URL:
            parsed = urllib.parse.urlsplit(target_url)
            if (parsed.scheme == "https" and parsed.hostname == "mirror.accum.se"
                    and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                    and parsed.path == "/mirror/qt.io/qtproject/archive/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz"
                    and not parsed.query and not parsed.fragment):
                return
        raise SourceAssetError(f"Qt source redirect is not the specifically approved mirror URL: {target_url}")
    if source_url == QT_APPROVED_MIRROR_URL:
        if target_url in {source_url, QT_APPROVED_BACKEND_URL}:
            return
        raise SourceAssetError(f"Qt mirror redirect is not the specifically approved backend URL: {target_url}")
    _validate_https_url(target_url)


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_redirect_target(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str):
    _validate_https_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "NovaMicSwitch-source-assets/1"})
    opener = urllib.request.build_opener(_AllowlistedRedirectHandler())
    try:
        return opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except SourceAssetError as exc:
        # Qt's official host currently redirects to an unapproved regional mirror.
        # Never follow that Location; retry only the independently reviewed exact mirror URL.
        if url != QT_CANONICAL_URL or "Qt source redirect is not the specifically approved mirror URL" not in str(exc):
            raise
        mirror_request = urllib.request.Request(
            QT_APPROVED_MIRROR_URL, headers={"User-Agent": "NovaMicSwitch-source-assets/1"}
        )
        return opener.open(mirror_request, timeout=DOWNLOAD_TIMEOUT_SECONDS)


def _response_url(response, requested_url: str) -> str:
    final_url = response.geturl() if hasattr(response, "geturl") else requested_url
    if requested_url == QT_CANONICAL_URL and final_url == QT_APPROVED_BACKEND_URL:
        return final_url
    _validate_redirect_target(requested_url, final_url)
    return final_url


def _read_small(url: str, *, limit: int = MAX_METADATA_BYTES) -> tuple[bytes, str]:
    try:
        with _open_url(url) as response:
            final_url = _response_url(response, url)
            declared = _content_length(response)
            if declared is not None and declared > limit:
                raise SourceAssetError(f"Metadata response exceeds {limit} bytes: {url}")
            payload = response.read(limit + 1)
        if not payload or len(payload) > limit:
            raise SourceAssetError(f"Metadata response is empty or exceeds {limit} bytes: {url}")
        if declared is not None and len(payload) != declared:
            raise SourceAssetError(f"Metadata response byte count does not match Content-Length: {url}")
        return payload, final_url
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, SourceAssetError):
            raise
        raise SourceAssetError(f"Could not read source metadata from {url}: {exc}") from exc


def _content_length(response) -> int | None:
    raw_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise SourceAssetError("HTTP response has an invalid Content-Length") from exc
    if length < 0:
        raise SourceAssetError("HTTP response has a negative Content-Length")
    return length


def _read_config(config_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceAssetError(f"Could not read source asset pins from {config_path}: {exc}") from exc
    if (not isinstance(raw, dict) or set(raw) != {"schema_version", "assets", "qt_ffmpeg_recipes"}
            or type(raw["schema_version"]) is not int or raw["schema_version"] != 1):
        raise SourceAssetError("Source asset configuration must use the strict schema_version 1 format.")
    if not isinstance(raw["assets"], list):
        raise SourceAssetError("Source asset configuration 'assets' must be a list.")
    assets: dict[str, dict[str, Any]] = {}
    for entry in raw["assets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise SourceAssetError("Invalid source asset definition.")
        asset_id = entry["id"]
        if asset_id in assets or asset_id not in EXPECTED_ASSETS:
            raise SourceAssetError(f"Unknown or duplicate source asset ID: {asset_id}")
        expected = EXPECTED_ASSETS[asset_id]
        for key, value in expected.items():
            if entry.get(key) != value:
                raise SourceAssetError(f"Source asset {asset_id!r} has an unexpected {key} pin.")
        if not isinstance(entry.get("verification_basis"), str) or not entry["verification_basis"].strip():
            raise SourceAssetError(f"Source asset {asset_id!r} must describe its verification basis.")
        if type(entry.get("max_bytes")) is not int or not 1 <= entry["max_bytes"] <= MAX_ASSET_BYTES:
            raise SourceAssetError(f"Source asset {asset_id!r} has an invalid size limit.")
        if "size_bytes" in expected and (type(entry.get("size_bytes")) is not int
                                          or entry["size_bytes"] != expected["size_bytes"]):
            raise SourceAssetError(f"Source asset {asset_id!r} has an invalid exact size pin.")
        digest = entry.get("sha256")
        if digest is not None and (not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None):
            raise SourceAssetError(f"Source asset {asset_id!r} SHA-256 pin is invalid.")
        _validate_https_url(entry["url"])
        assets[asset_id] = dict(entry)
    if set(assets) != set(EXPECTED_ASSETS):
        raise SourceAssetError("Configuration must include exactly the four approved source archives.")
    recipes = raw["qt_ffmpeg_recipes"]
    if (not isinstance(recipes, dict)
            or set(recipes) != {"repository", "tag", "tag_object", "resolved_commit", "archive_filename", "sha256", "verification_basis", "files"}
            or recipes.get("repository") != "qt/qt5" or recipes.get("tag") != "v6.12.0"
            or recipes.get("tag_object") != "41737d37054a9c500b2d0df38199593accb8b5c0"
            or recipes.get("archive_filename") != "qt-6.12.0-ffmpeg-recipes.tar.gz"
            or not isinstance(recipes.get("verification_basis"), str)
            or not recipes["verification_basis"].strip()
            or not isinstance(recipes.get("files"), list)):
        raise SourceAssetError("Invalid Qt v6.12.0 FFmpeg recipe definition.")
    recipe_commit = recipes.get("resolved_commit")
    if recipe_commit is not None and (not isinstance(recipe_commit, str) or COMMIT_RE.fullmatch(recipe_commit) is None):
        raise SourceAssetError("Qt v6.12.0 recipe resolved_commit must be a lowercase 40-character commit or null.")
    if len(recipes["files"]) != len(RECIPE_PATHS):
        raise SourceAssetError("Qt v6.12.0 recipe file list does not match the approved exact file set.")
    for item, expected_path in zip(recipes["files"], RECIPE_PATHS):
        if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                or item["path"] != expected_path
                or item["sha256"] is not None and (not isinstance(item["sha256"], str)
                                                    or SHA256_RE.fullmatch(item["sha256"]) is None)):
            raise SourceAssetError(f"Invalid or unexpected Qt recipe entry for {expected_path}.")
    recipe_archive_sha = recipes.get("sha256")
    if recipe_archive_sha is not None and (not isinstance(recipe_archive_sha, str)
                                           or SHA256_RE.fullmatch(recipe_archive_sha) is None):
        raise SourceAssetError("Qt recipe archive SHA-256 pin is invalid.")
    if set(recipes) != {"repository", "tag", "tag_object", "resolved_commit", "archive_filename", "verification_basis", "files", "sha256"}:
        raise SourceAssetError("Qt recipe definition has missing or unexpected fields.")
    return list(assets.values()), dict(recipes)


def _hash_file(path: Path) -> tuple[str, str, int]:
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(chunk)
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest(), count


QT5_REQUIRED_HELPER_NAMES = frozenset({
    "helpers.ps1", "zlib-helpers.ps1", "InstallFromCompressedFileFromURL.sh", "SetEnvVar.sh",
})


def _validate_qt5_source_archive(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            names = set()
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise SourceAssetError(f"Qt qt5 source archive has an unsafe member path: {member.name}")
                if member.isfile():
                    names.add(name.name)
    except (OSError, tarfile.TarError) as exc:
        raise SourceAssetError(f"Could not read Qt qt5 supermodule archive {path}: {exc}") from exc
    missing = sorted(QT5_REQUIRED_HELPER_NAMES - names)
    if missing:
        raise SourceAssetError(f"Qt qt5 supermodule archive is missing referenced recipe helpers: {', '.join(missing)}")


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cache_manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "source-cache.json"


def _read_cache_manifest(cache_dir: Path) -> dict[str, Any]:
    path = _cache_manifest_path(cache_dir)
    if path.is_symlink():
        raise SourceAssetError("Source cache verification manifest must not be a symlink.")
    if not path.exists():
        return {"schema_version": 1, "assets": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceAssetError(f"Could not read source cache verification records: {exc}") from exc
    if (not isinstance(raw, dict) or set(raw) != {"schema_version", "assets"}
            or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
            or not isinstance(raw["assets"], dict)):
        raise SourceAssetError("Source cache verification manifest is invalid.")
    return raw


def _record_cache(cache_dir: Path, asset_id: str, record: dict[str, Any]) -> None:
    manifest = _read_cache_manifest(cache_dir)
    manifest["assets"][asset_id] = record
    _atomic_json(_cache_manifest_path(cache_dir), manifest)


def _ensure_cache_parent(cache_dir: Path, parent: Path) -> None:
    try:
        relative = parent.relative_to(cache_dir)
    except ValueError as exc:
        raise SourceAssetError("Source cache path escaped its configured cache directory.") from exc
    current = cache_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SourceAssetError(f"Source cache directory must not contain symlinks: {current}")
        current.mkdir(exist_ok=True)


def _existing_record(cache_dir: Path, asset_id: str, url: str) -> dict[str, Any] | None:
    record = _read_cache_manifest(cache_dir)["assets"].get(asset_id)
    if record is None:
        return None
    if (not isinstance(record, dict) or record.get("url") != url
            or not isinstance(record.get("sha256"), str) or SHA256_RE.fullmatch(record["sha256"]) is None
            or type(record.get("size_bytes")) is not int or record["size_bytes"] < 1):
        raise SourceAssetError(f"Cached source verification record is invalid for {asset_id}.")
    return record


def _download_file(url: str, target: Path, *, max_bytes: int, expected_sha256: str | None = None,
                   expected_sha1: str | None = None) -> dict[str, Any]:
    _validate_https_url(url)
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _open_url(url) as response:
            final_url = _response_url(response, url)
            declared = _content_length(response)
            if declared is not None and declared > max_bytes:
                raise SourceAssetError(f"Source asset exceeds {max_bytes} bytes: {url}")
            sha256 = hashlib.sha256()
            sha1 = hashlib.sha1()
            count = 0
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix=f".{target.name}.",
                                             suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > max_bytes:
                        raise SourceAssetError(f"Source asset exceeds {max_bytes} bytes: {url}")
                    sha256.update(chunk)
                    sha1.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if count == 0:
            raise SourceAssetError(f"Downloaded source asset is empty: {url}")
        if declared is not None and count != declared:
            raise SourceAssetError(f"Downloaded byte count does not match Content-Length: {url}")
        actual_sha256, actual_sha1 = sha256.hexdigest(), sha1.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise SourceAssetError(f"Downloaded source asset SHA-256 does not match its pin: {url}")
        if expected_sha1 is not None and actual_sha1 != expected_sha1:
            raise SourceAssetError(f"Downloaded source asset SHA-1 does not match the Qt release recipe: {url}")
        os.replace(temporary, target)
        temporary = None
        return {"url": url, "final_url": final_url, "sha256": actual_sha256,
                "sha1": actual_sha1, "size_bytes": count}
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, SourceAssetError):
            raise
        raise SourceAssetError(f"Could not fetch source asset from {url}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _qt_official_sha256(asset: dict[str, Any]) -> tuple[str | None, str | None]:
    filename = asset["filename"]
    base = asset["url"].rsplit("/", 1)[0]
    candidates = (
        f"{asset['url']}.sha256",
        f"{base}/sha256sums.txt",
        f"{base}/sha256sums",
    )
    for url in candidates:
        try:
            payload, final_url = _read_small(url)
        except SourceAssetError as exc:
            # A missing checksum index is not a reason to fetch from another host.
            cause = exc.__cause__
            if isinstance(cause, urllib.error.HTTPError) and cause.code == 404:
                continue
            if "HTTP Error 404" in str(exc):
                continue
            raise
        text = payload.decode("ascii", errors="replace")
        for line in text.splitlines():
            match = re.match(r"^\s*([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$", line)
            if match and PurePosixPath(match.group(2).lstrip("* ")).name == filename:
                return match.group(1).lower(), final_url
    return None, None


def _resolve_qt_commit(expected_tag_object: str) -> str:
    payload, _ = _read_small(QT_TAG_URL)
    try:
        ref = json.loads(payload)
        obj = ref["object"]
        object_type, object_sha = obj["type"], obj["sha"]
        if object_type != "tag" or object_sha != expected_tag_object:
            raise ValueError("v6.12.0 tag ref does not match the reviewed annotated tag object")
        tag_url = f"https://api.github.com/repos/qt/qt5/git/tags/{object_sha}"
        tag_payload, _ = _read_small(tag_url)
        tag_obj = json.loads(tag_payload)["object"]
        object_type, object_sha = tag_obj["type"], tag_obj["sha"]
        if object_type != "commit" or not isinstance(object_sha, str) or COMMIT_RE.fullmatch(object_sha) is None:
            raise ValueError("tag does not resolve to a commit")
        return object_sha
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceAssetError(f"Could not resolve official Qt v6.12.0 tag to a commit: {exc}") from exc


def _get_recipe_files(cache_dir: Path, recipes: dict[str, Any], *, fetch: bool) -> tuple[str, list[dict[str, Any]]]:
    commit = recipes["resolved_commit"]
    if commit is None:
        if not fetch:
            raise SourceAssetError("Qt v6.12.0 recipe commit is not pinned. Rerun with --fetch to resolve the official tag.")
        commit = _resolve_qt_commit(recipes["tag_object"])
    root_url = "https://raw.githubusercontent.com/qt/qt5/"
    material = []
    for entry in recipes["files"]:
        relative = entry["path"]
        url = f"{root_url}{commit}/{relative}"
        cache_id = f"qt5-recipe:{relative}"
        path = cache_dir / "qt5-recipes" / commit / relative
        pin = entry["sha256"]
        record = _existing_record(cache_dir, cache_id, url) if path.exists() else None
        if path.exists():
            if not path.is_file() or path.is_symlink() or record is None:
                raise SourceAssetError(f"Cached Qt recipe is missing its verified record: {path}")
            digest, _, size = _hash_file(path)
            if digest != record["sha256"] or size != record["size_bytes"] or (pin and digest != pin):
                raise SourceAssetError(f"Cached Qt recipe failed its SHA-256 pin: {path}")
        else:
            if not fetch:
                raise SourceAssetError(f"Missing cached Qt recipe {relative}. Rerun with --fetch to download it.")
            _ensure_cache_parent(cache_dir, path.parent)
            if pin is not None:
                expected = pin
            else:
                expected = None
            record = _download_file(url, path, max_bytes=MAX_RECIPE_BYTES, expected_sha256=expected)
            record.update(source_commit=commit)
            _record_cache(cache_dir, cache_id, record)
        if pin is not None and record["sha256"] != pin:
            raise SourceAssetError(f"Qt recipe {relative} does not match its committed SHA-256 pin.")
        material.append({"path": relative, "url": url, "sha256": record["sha256"],
                         "size_bytes": record["size_bytes"], "cache_path": path})
    return commit, material


def _make_recipe_archive(path: Path, commit: str, material: list[dict[str, Any]], basis: str) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "source": "qt/qt5",
        "tag": "v6.12.0",
        "resolved_commit": commit,
        "archive_filename": "qt-6.12.0-ffmpeg-recipes.tar.gz",
        "verification_basis": basis,
        "files": [{key: value for key, value in row.items() if key != "cache_path"} for row in material],
        "scope_note": "Four version-matched FFmpeg provisioning recipe files only; this is not a complete Qt source/build environment.",
    }
    prefix = f"qt5-ffmpeg-recipes-{commit}"
    with path.open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, mtime=0, compresslevel=9) as gzip_stream:
            with tarfile.open(fileobj=gzip_stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
                entries = [("SOURCE-MATERIAL.json", manifest_bytes, 0o644)]
                for row in material:
                    entries.append((row["path"], row["cache_path"].read_bytes(), 0o644))
                for relative, data, mode in entries:
                    info = tarfile.TarInfo(f"{prefix}/{relative}")
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = mode
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
        raw_stream.flush()
        os.fsync(raw_stream.fileno())
    sha256, sha1, size = _hash_file(path)
    return {"sha256": sha256, "sha1": sha1, "size_bytes": size, "manifest": manifest}


def _verified_cached_asset(asset: dict[str, Any], cache_dir: Path, *, fetch: bool,
                           official_sha256: str | None = None, official_sha_url: str | None = None) -> dict[str, Any]:
    asset_id, url = asset["id"], asset["url"]
    target = cache_dir / asset["filename"]
    pin = asset["sha256"]
    if official_sha256 is not None:
        if pin is not None and pin != official_sha256:
            raise SourceAssetError(f"Configured SHA-256 for {asset_id} disagrees with the official checksum file.")
        pin = official_sha256
    record = _existing_record(cache_dir, asset_id, url) if target.exists() else None
    if target.exists():
        if target.is_symlink() or not target.is_file() or record is None:
            raise SourceAssetError(f"Cached {asset_id} file is missing its verified cache record: {target}")
        sha256, sha1, size = _hash_file(target)
        if size != record["size_bytes"] or sha256 != record["sha256"]:
            raise SourceAssetError(f"Cached {asset_id} file failed its recorded size/SHA-256 check: {target}")
        if pin is not None and sha256 != pin:
            raise SourceAssetError(f"Cached {asset_id} file failed its committed SHA-256 pin: {target}")
        if asset.get("sha1") is not None and sha1 != asset["sha1"]:
            raise SourceAssetError(f"Cached {asset_id} file failed the Qt recipe SHA-1 check: {target}")
        result = dict(record, sha1=sha1)
    else:
        if not fetch:
            raise SourceAssetError(f"Missing cached source asset {asset['filename']}. Rerun with --fetch to download it.")
        _ensure_cache_parent(cache_dir, target.parent)
        result = _download_file(url, target, max_bytes=asset["max_bytes"], expected_sha256=pin,
                                expected_sha1=asset.get("sha1"))
        result["verification_basis"] = asset["verification_basis"]
        if official_sha_url:
            result["upstream_sha256_url"] = official_sha_url
        record = dict(result)
        record["source_commit"] = asset.get("source_commit")
        _record_cache(cache_dir, asset_id, record)
    expected_size = asset.get("size_bytes")
    if expected_size is not None and result["size_bytes"] != expected_size:
        raise SourceAssetError(f"Source asset {asset_id} has {result['size_bytes']} bytes, not pinned size {expected_size}.")
    if asset["id"] == "ffmpeg" and asset.get("sha1") != result["sha1"]:
        raise SourceAssetError("FFmpeg source SHA-1 does not match the exact Qt v6.12.0 recipe reference.")
    if asset["id"] == "qt5-supermodule-source":
        _validate_qt5_source_archive(target)
    result.update(asset_id=asset_id, filename=asset["filename"], version=asset["version"],
                  source_commit=asset.get("source_commit"), verification_basis=asset["verification_basis"])
    if official_sha256:
        result["verification_basis"] = "Official versioned HTTPS Qt source archive; SHA-256 verified against the official Qt checksum file."
        result["upstream_sha256_url"] = official_sha_url
    elif asset.get("id") == "qt-everywhere-src":
        result["verification_basis"] = (
            "Official versioned HTTPS Qt source URL and locally measured SHA-256; no matching official SHA-256 checksum entry was available."
        )
    elif asset.get("id") == "ffmpeg":
        result["verification_basis"] = (
            "Official FFmpeg versioned HTTPS source URL; SHA-1 matches the exact Qt v6.12.0 recipe; SHA-256 measured locally, not an upstream cryptographic attestation."
        )
    return result


def _copy_unchanged(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise SourceAssetError(f"Refusing to replace non-regular source output path: {destination}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=destination.parent, prefix=f".{destination.name}.",
                                         suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
        temporary.unlink()
        try:
            os.link(source, temporary)
        except OSError:
            with source.open("rb") as source_stream, temporary.open("wb") as output_stream:
                shutil.copyfileobj(source_stream, output_stream, length=1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise SourceAssetError(f"Could not stage source archive {source.name}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_output(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SourceAssetError(f"Refusing to replace non-regular source index output: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".part",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise SourceAssetError(f"Could not atomically write {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _render_markdown(assets: list[dict[str, Any]], recipe_archive: dict[str, Any]) -> bytes:
    lines = [
        "# Upstream source assets",
        "",
        "Locally staged source archives; no archive has been extracted, repacked, or executed.",
        "Digests identify the bytes staged here. They do not imply a vendor-published checksum unless the verification basis says so.",
        "",
        "| Asset | Version / commit | Bytes | SHA-256 | Verification basis |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for item in assets:
        version = item["version"]
        if item.get("source_commit"):
            version += f" / `{item['source_commit']}`"
        basis = item["verification_basis"].replace("|", "\\|")
        lines.append(f"| `{item['filename']}` | {version} | {item['size_bytes']} | `{item['sha256']}` | {basis} |")
    recipe_manifest = recipe_archive["manifest"]
    lines.extend([
        f"| `{recipe_manifest['archive_filename']}` | v6.12.0 / `{recipe_manifest['resolved_commit']}` | {recipe_archive['size_bytes']} | `{recipe_archive['sha256']}` | {recipe_manifest['verification_basis']} |",
        "",
        "## Qt v6.12.0 FFmpeg provisioning recipe subset",
        "",
        "The deterministic recipe archive preserves these files' bytes and repository paths. It is not a complete Qt build environment; additional shared-script references and build inputs have not been inventoried.",
    ])
    for entry in recipe_manifest["files"]:
        lines.append(f"- `{entry['path']}` — [{entry['url']}]({entry['url']}), SHA-256 `{entry['sha256']}`, {entry['size_bytes']} bytes")
    lines.extend(["", "## Limitations", "", "This source set covers Qt 6.12.0, the Qt qt5 v6.12.0 supermodule, the PySide/shiboken v6.12.0 commit, FFmpeg 9.0.1, and four Qt v6.12.0 FFmpeg provisioning recipe files. It does not establish a complete build environment or include runtime/system source packages and other copyleft inputs.", ""])
    return ("\n".join(lines)).encode("utf-8")


def stage_source_assets(cache_dir: Path, output_dir: Path, *, fetch: bool = False,
                        config_path: Path = DEFAULT_CONFIG) -> list[dict[str, Any]]:
    assets, recipes = _read_config(Path(config_path))
    cache_dir, output_dir = Path(cache_dir), Path(output_dir)
    if cache_dir.is_symlink() or output_dir.is_symlink():
        raise SourceAssetError("Source cache and output directories must not be symlinks.")
    if fetch:
        cache_dir.mkdir(parents=True, exist_ok=True)

    recipe_commit, recipe_material = _get_recipe_files(cache_dir, recipes, fetch=fetch)
    recipe_temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=cache_dir, prefix=".qt-recipes.", suffix=".part", delete=False) as stream:
            recipe_temp = Path(stream.name)
        recipe_archive = _make_recipe_archive(recipe_temp, recipe_commit, recipe_material, recipes["verification_basis"])
        recipe_pin = recipes.get("sha256")
        if recipe_pin is not None and recipe_pin != recipe_archive["sha256"]:
            raise SourceAssetError("Generated Qt recipe archive does not match its committed SHA-256 pin.")
        recipe_cache_id = "qt5-ffmpeg-recipes"
        recipe_cache_path = cache_dir / recipes["archive_filename"]
        record = _existing_record(cache_dir, recipe_cache_id, f"qt/qt5@{recipe_commit}:v6.12.0") if recipe_cache_path.exists() else None
        if recipe_cache_path.exists():
            if recipe_cache_path.is_symlink() or not recipe_cache_path.is_file() or record is None:
                raise SourceAssetError("Cached Qt recipe archive is missing its verified cache record.")
            old_hash, _, old_size = _hash_file(recipe_cache_path)
            if old_hash != record["sha256"] or old_size != record["size_bytes"] or old_hash != recipe_archive["sha256"]:
                raise SourceAssetError("Cached Qt recipe archive differs from the verified recipe material.")
        else:
            if not fetch:
                raise SourceAssetError(f"Missing cached source asset {recipes['archive_filename']}. Rerun with --fetch to create it.")
            os.replace(recipe_temp, recipe_cache_path)
            recipe_temp = None
            record = {"url": f"qt/qt5@{recipe_commit}:v6.12.0", "sha256": recipe_archive["sha256"],
                      "size_bytes": recipe_archive["size_bytes"], "source_commit": recipe_commit}
            _record_cache(cache_dir, recipe_cache_id, record)
        recipe_index = {
            "filename": recipes["archive_filename"], "version": "6.12.0",
            "tag_object": recipes["tag_object"], "source_commit": recipe_commit,
            "sha256": recipe_archive["sha256"], "size_bytes": recipe_archive["size_bytes"],
            "verification_basis": recipes["verification_basis"], "manifest": recipe_archive["manifest"],
        }

        indexed_assets = []
        for asset in assets:
            official_sha, official_sha_url = (None, None)
            if asset["id"] == "qt-everywhere-src" and fetch:
                official_sha, official_sha_url = _qt_official_sha256(asset)
            item = _verified_cached_asset(asset, cache_dir, fetch=fetch,
                                          official_sha256=official_sha, official_sha_url=official_sha_url)
            indexed_assets.append(item)

        # Build all outputs in a private directory under output_dir before atomically replacing named outputs.
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".source-assets-", dir=output_dir) as stage_name:
            stage = Path(stage_name)
            output_rows = []
            for item in indexed_assets:
                staged = stage / item["filename"]
                _copy_unchanged(cache_dir / item["filename"], staged)
                output_rows.append((staged, output_dir / item["filename"]))
            recipe_staged = stage / recipe_index["filename"]
            _copy_unchanged(recipe_cache_path, recipe_staged)
            output_rows.append((recipe_staged, output_dir / recipe_index["filename"]))

            all_indexed = indexed_assets + [{
                "asset_id": "qt5-ffmpeg-recipes", **recipe_index,
                "url": f"https://api.github.com/repos/qt/qt5/git/ref/tags/v6.12.0",
            }]
            json_bytes = (json.dumps({
                "schema_version": 1,
                "assets": [{key: value for key, value in item.items() if key not in {"cache_path", "sha1"}}
                           for item in all_indexed],
            }, indent=2, sort_keys=True) + "\n").encode("utf-8")
            markdown_bytes = _render_markdown(indexed_assets, recipe_index)
            checksums = []
            for item in all_indexed:
                checksums.append(f"{item['sha256']}  {item['filename']}\n")
            for path, data in ((stage / "source-index.json", json_bytes),
                               (stage / "SOURCE-INDEX.md", markdown_bytes),
                               (stage / "SHA256SUMS", "".join(checksums).encode("ascii"))):
                path.write_bytes(data)
                output_rows.append((path, output_dir / path.name))
            for staged, destination in output_rows:
                if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                    raise SourceAssetError(f"Refusing to replace non-regular source output path: {destination}")
            for staged, destination in output_rows:
                os.replace(staged, destination)
        return all_indexed
    finally:
        if recipe_temp is not None:
            recipe_temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true", help="explicitly allow fetching missing pinned source material")
    args = parser.parse_args(argv)
    try:
        assets = stage_source_assets(args.cache_dir, args.output_dir, fetch=args.fetch)
    except SourceAssetError as exc:
        print(f"build_source_assets: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"build_source_assets: filesystem error: {exc}", file=sys.stderr)
        return 2
    for item in assets:
        print(f"{item['filename']}\t{item['size_bytes']} bytes\tSHA-256 {item['sha256']}")
    print(f"Source assets staged in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
