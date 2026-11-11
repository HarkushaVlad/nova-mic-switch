#!/usr/bin/env python3
"""Fetch pinned release build tools into a project-local cache."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "packaging" / "release-tools.json"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 45
KNOWN_TOOLS = {
    "appimagetool": {
        "filename": "appimagetool-x86_64.AppImage",
        "url": "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage",
    },
    "inno-setup": {
        "filename": "innosetup-6.7.4.exe",
        "url": "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe",
    },
    "appimage-runtime": {
        "filename": "runtime-x86_64",
        "url": "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64",
        "size_bytes": 944632,
        "source_commit": "dd6cebedcbddde9c82f89b011e8e1d40b6e43868",
    },
}
TOOL_CHOICES = tuple(KNOWN_TOOLS)


class ReleaseToolError(RuntimeError):
    """Invalid tool pin, download, or cached tool error."""


def _read_config(config_path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseToolError(f"Could not read release tool pins from {config_path}: {exc}") from exc
    if (not isinstance(raw, dict) or set(raw) != {"schema_version", "tools"}
            or type(raw["schema_version"]) is not int or raw["schema_version"] != 1):
        raise ReleaseToolError("Release tool configuration must use the strict schema_version 1 format.")
    tools = raw["tools"]
    if not isinstance(tools, dict):
        raise ReleaseToolError("The release tool configuration 'tools' field must be an object.")
    if set(tools) - set(KNOWN_TOOLS):
        raise ReleaseToolError("The release tool configuration contains an unknown tool name.")
    parsed: dict[str, dict[str, Any]] = {}
    for name, entry in tools.items():
        expected_fields = {"url", "sha256"}
        if "size_bytes" in KNOWN_TOOLS[name]:
            expected_fields.update(("size_bytes", "source_commit"))
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ReleaseToolError(f"Tool pin for {name!r} has missing or unexpected fields.")
        url, digest = entry["url"], entry["sha256"]
        if not isinstance(url, str) or url != KNOWN_TOOLS[name]["url"] or not url.startswith("https://github.com/"):
            raise ReleaseToolError(f"Tool {name!r} URL is not an approved official GitHub release URL.")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReleaseToolError(f"Tool {name!r} SHA-256 must be exactly 64 lowercase hexadecimal characters.")
        item: dict[str, Any] = {"url": url, "sha256": digest}
        if "size_bytes" in expected_fields:
            size = entry["size_bytes"]
            source_commit = entry["source_commit"]
            if type(size) is not int or size != KNOWN_TOOLS[name]["size_bytes"]:
                raise ReleaseToolError(f"Tool {name!r} size_bytes must match the reviewed upstream asset size.")
            if source_commit != KNOWN_TOOLS[name]["source_commit"]:
                raise ReleaseToolError(f"Tool {name!r} source_commit does not match the reviewed release source.")
            item.update(size_bytes=size, source_commit=source_commit)
        parsed[name] = item
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_tool(tool: str, output_dir: Path, *, config_path: Path = DEFAULT_CONFIG) -> Path:
    if tool not in TOOL_CHOICES:
        raise ReleaseToolError(f"Unknown release tool {tool!r}.")
    pins = _read_config(config_path)
    if tool not in pins:
        raise ReleaseToolError(
            f"No verified pin is configured for {tool}."
        )
    info = KNOWN_TOOLS[tool]
    output_dir = Path(output_dir)
    target = output_dir / info["filename"]
    expected = pins[tool]["sha256"]
    expected_size = pins[tool].get("size_bytes")
    if target.is_symlink() or target.exists():
        if not target.is_file():
            raise ReleaseToolError(
                f"Cached {tool} file failed its pinned SHA-256 check: {target}. Remove it explicitly to redownload."
            )
        try:
            cached_size = target.stat().st_size
            cached_hash = _sha256(target)
        except OSError as exc:
            raise ReleaseToolError(f"Could not verify cached {tool} file {target}: {exc}") from exc
        if (expected_size is not None and cached_size != expected_size) or cached_hash != expected:
            raise ReleaseToolError(
                f"Cached {tool} file failed its pinned size or SHA-256 check: {target}. Remove it explicitly to redownload."
            )
        if tool == "appimagetool":
            try:
                target.chmod(0o755)
            except OSError as exc:
                raise ReleaseToolError(f"Could not mark verified appimagetool executable: {exc}") from exc
        return target

    temporary: Path | None = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            pins[tool]["url"], headers={"User-Agent": "NovaMicSwitch-release-tools/1"}
        )
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            parsed_url = urllib.parse.urlparse(final_url)
            if parsed_url.scheme != "https" or parsed_url.hostname not in {
                "github.com", "release-assets.githubusercontent.com"
            }:
                raise ReleaseToolError("Release tool download redirected outside the approved GitHub HTTPS host.")
            length = response.headers.get("Content-Length")
            declared_length = int(length) if length is not None else None
            if declared_length is not None and declared_length > MAX_DOWNLOAD_BYTES:
                raise ReleaseToolError("Release tool download exceeds the configured size limit.")
            if expected_size is not None and declared_length is not None and declared_length != expected_size:
                raise ReleaseToolError(f"Downloaded {tool} Content-Length does not match its pinned size.")
            digest = hashlib.sha256()
            count = 0
            with tempfile.NamedTemporaryFile("wb", dir=output_dir, prefix=f".{info['filename']}.",
                                             suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_DOWNLOAD_BYTES:
                        raise ReleaseToolError("Release tool download exceeds the configured size limit.")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if expected_size is not None and count != expected_size:
            raise ReleaseToolError(f"Downloaded {tool} file does not match its pinned size.")
        if declared_length is not None and count != declared_length:
            raise ReleaseToolError(f"Downloaded {tool} byte count does not match its Content-Length.")
        if digest.hexdigest() != expected:
            raise ReleaseToolError(f"Downloaded {tool} file does not match its pinned SHA-256 digest.")
        os.replace(temporary, target)
        temporary = None
        if tool == "appimagetool":
            try:
                target.chmod(0o755)
            except OSError as exc:
                raise ReleaseToolError(f"Could not mark verified appimagetool executable: {exc}") from exc
        return target
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, ReleaseToolError):
            raise
        raise ReleaseToolError(f"Could not fetch verified release tool {tool}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", choices=TOOL_CHOICES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        print(fetch_tool(args.tool, args.output_dir, config_path=args.config))
    except ReleaseToolError as exc:
        print(f"release_tools: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
