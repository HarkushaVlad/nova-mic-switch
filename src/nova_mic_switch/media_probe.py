"""Bounded, silent Qt Multimedia/FFmpeg backend diagnostic for frozen builds."""

from __future__ import annotations

import argparse
import ctypes
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import time
import wave

DEADLINE_SECONDS = 12
EXPECTED_FORMAT = "WAVE"
EXPECTED_CODEC = "WAVE"
FFMPEG_LIBRARIES = ("avutil", "avcodec", "avformat", "swresample", "swscale")


def _library_candidates(root: Path, *, frozen: bool) -> list[Path]:
    patterns = ("*avutil*", "*avcodec*", "*avformat*", "*swresample*", "*swscale*")
    native_name = re.compile(r"^(?:lib)?(?:avutil|avcodec|avformat|swresample|swscale)(?:[-.]|$)", re.I)
    native_suffix = re.compile(r"(?:\.so(?:\..*)?|\.dll|\.dylib)$", re.I)
    paths = {Path(item).resolve() for pattern in patterns for item in glob.glob(str(root / "**" / pattern), recursive=True)}
    paths = sorted(
        path for path in paths
        if path.is_file()
        and native_name.search(path.name)
        and native_suffix.search(path.name)
        and (not frozen or path.is_relative_to(root.resolve()))
    )
    return paths


def _query_ffmpeg(root: Path, *, frozen: bool) -> tuple[dict[str, object], list[object]]:
    windows_handles: list[object] = []
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        windows_handles.append(os.add_dll_directory(str(root)))
    result: dict[str, object] = {"root": str(root), "frozen": frozen, "libraries": [], "loaded_paths": []}
    handles: list[object] = []
    failures: list[dict[str, str]] = []
    loaded_families: set[str] = set()
    for path in _library_candidates(root, frozen=frozen):
        family_match = re.search(r"(?:lib)?(avutil|avcodec|avformat|swresample|swscale)", path.name, re.I)
        if family_match and family_match.group(1).lower() in loaded_families:
            continue
        try:
            handle = ctypes.CDLL(str(path))
            handles.append(handle)
            entry: dict[str, object] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            short = re.search(r"(?:lib)?(avutil|avcodec|avformat|swresample|swscale)", path.name, re.I)
            if short:
                loaded_families.add(short.group(1).lower())
                try:
                    version_fn = getattr(handle, short.group(1) + "_version")
                    version_fn.argtypes = []
                    version_fn.restype = ctypes.c_uint
                    version = int(version_fn())
                    entry["version"] = version
                    entry["version_text"] = f"{version >> 16}.{(version >> 8) & 0xff}.{version & 0xff}"
                except (AttributeError, OSError):
                    pass
                try:
                    config_fn = getattr(handle, short.group(1) + "_configuration")
                    config_fn.argtypes = []
                    config_fn.restype = ctypes.c_char_p
                    configuration = config_fn()
                    if configuration:
                        config_text = configuration.decode("utf-8", "replace")
                        entry["configuration"] = config_text
                        entry["lgpl_configuration"] = "--enable-gpl" not in config_text and "--enable-nonfree" not in config_text
                except (AttributeError, OSError):
                    pass
                try:
                    license_fn = getattr(handle, short.group(1) + "_license")
                    license_fn.argtypes = []
                    license_fn.restype = ctypes.c_char_p
                    license_text = license_fn()
                    if license_text:
                        entry["license"] = license_text.decode("utf-8", "replace")
                except (AttributeError, OSError):
                    pass
            result["libraries"].append(entry)  # type: ignore[union-attr]
        except OSError as error:
            failures.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
            continue
    result["loaded_paths"] = _loaded_native_paths()
    result["load_failures"] = failures
    return result, handles + windows_handles


def _loaded_native_paths() -> list[str]:
    if os.name == "nt":
        try:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process = kernel32.GetCurrentProcess
            process.argtypes = []
            process.restype = ctypes.c_void_p
            modules = (ctypes.c_void_p * 2048)()
            needed = ctypes.c_ulong()
            enum = psapi.EnumProcessModules
            enum.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
            enum.restype = ctypes.c_int
            if not enum(process(), modules, ctypes.sizeof(modules), ctypes.byref(needed)):
                return []
            get_name = psapi.GetModuleFileNameExW
            get_name.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
            get_name.restype = ctypes.c_ulong
            matches = set()
            count = min(needed.value // ctypes.sizeof(ctypes.c_void_p), len(modules))
            for module in modules[:count]:
                buffer = ctypes.create_unicode_buffer(32768)
                if get_name(process(), module, buffer, len(buffer)) and re.search(
                    r"(avutil|avcodec|avformat|swresample|swscale|ffmpeg|crypto|ssl|Qt6Multimedia)", buffer.value, re.I
                ):
                    matches.add(buffer.value)
            return sorted(matches)
        except (AttributeError, OSError):
            return []
    maps = Path("/proc/self/maps")
    if not maps.is_file():
        return []
    matches = set()
    for line in maps.read_text(encoding="utf-8", errors="replace").splitlines():
        path = line.split()[-1] if line.split() else ""
        if path.startswith("/") and re.search(r"(avutil|avcodec|avformat|swresample|swscale|ffmpeg|crypto|ssl|libQt6Multimedia)", path, re.I):
            matches.add(path)
    return sorted(matches)


def _enum_name(value: object) -> str:
    return str(getattr(value, "name", value)).split(".")[-1].upper()


def _wave_capabilities(QtMultimedia: object, codecs: list[object], formats: list[object]) -> tuple[bool, bool]:
    wave_codec = QtMultimedia.QMediaFormat.AudioCodec.Wave
    wave_format = QtMultimedia.QMediaFormat.FileFormat.Wave
    codec_ok = wave_codec in codecs or any(_enum_name(item) == EXPECTED_CODEC for item in codecs)
    format_ok = wave_format in formats or any(_enum_name(item) == EXPECTED_FORMAT for item in formats)
    return codec_ok, format_ok


def _write_silent_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\0\0" * 800)


def _load_media(player: object, QtCore: object, QtMultimedia: object, source: Path, timeout_ms: int) -> dict[str, object]:
    loop = QtCore.QEventLoop()
    timeout = QtCore.QTimer()
    timeout.setSingleShot(True)
    state: dict[str, object] = {"status": "", "error": "", "timed_out": False}
    loaded_status = QtMultimedia.QMediaPlayer.MediaStatus.LoadedMedia
    invalid_status = QtMultimedia.QMediaPlayer.MediaStatus.InvalidMedia

    def finish(status: object = None) -> None:
        if status == loaded_status:
            state["status"] = "LoadedMedia"
            loop.quit()
        elif status == invalid_status:
            state["status"] = "InvalidMedia"
            loop.quit()

    def on_error(error: object, message: str) -> None:
        state["error"] = {"code": _enum_name(error), "message": str(message)}
        loop.quit()

    player.mediaStatusChanged.connect(finish)
    player.errorOccurred.connect(on_error)
    def on_timeout() -> None:
        state["timed_out"] = True
        loop.quit()

    timeout.timeout.connect(on_timeout)
    timeout.start(timeout_ms)
    try:
        player.setSource(QtCore.QUrl.fromLocalFile(str(source)))
        current = player.mediaStatus()
        if current in (loaded_status, invalid_status):
            finish(current)
        if not state["status"] and not state["error"]:
            loop.exec()
    finally:
        timeout.stop()
        for signal, callback in ((player.mediaStatusChanged, finish), (player.errorOccurred, on_error), (timeout.timeout, on_timeout)):
            try:
                signal.disconnect(callback)
            except (AttributeError, RuntimeError, TypeError):
                pass
    return state


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="NovaMicSwitch --media-probe")
    parser.add_argument("--output-json", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report: dict[str, object] = {
        "schema_version": 1,
        "diagnostic": "Qt-initialized silent local-media backend proof; not hardware playback proof",
        "platform": platform.platform(),
        "success": False,
        "backend": "ffmpeg",
        "media": {},
        "errors": [],
    }
    deadline_ms = DEADLINE_SECONDS * 1000
    started = time.monotonic()
    # Must precede importing Qt Multimedia; intentionally do not mask plugin/load failures.
    os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    output = args.output_json.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    player = loop = app = None
    ffmpeg_result: dict[str, object] | None = None
    prior_qt_handler = None
    qt_handler_installed = False
    QtCore = None
    native_handles: list[object] = []
    qt_messages: list[str] = []
    output_written = False
    try:
        from PySide6 import QtCore, QtMultimedia
        from PySide6.QtGui import QGuiApplication

        def qt_message_handler(message_type: object, context: object, message: str) -> None:
            qt_messages.append(str(message))

        prior_qt_handler = QtCore.qInstallMessageHandler(qt_message_handler)
        qt_handler_installed = True
        report["qt_version"] = QtCore.qVersion()
        app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
        media_format = QtMultimedia.QMediaFormat()
        decode = QtMultimedia.QMediaFormat.ConversionMode.Decode
        supported_codecs = media_format.supportedAudioCodecs(decode)
        supported_formats = media_format.supportedFileFormats(decode)
        codecs = [_enum_name(item) for item in supported_codecs]
        formats = [_enum_name(item) for item in supported_formats]
        report["discovery"] = {"decode_audio_codecs": codecs, "decode_file_formats": formats}
        codec_available, format_available = _wave_capabilities(QtMultimedia, supported_codecs, supported_formats)
        if not codec_available or not format_available:
            raise RuntimeError(f"Expected WAV/PCM decode capability missing (codec={codec_available}, format={format_available})")
        remaining_ms = deadline_ms - int((time.monotonic() - started) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError(f"Qt backend initialization exceeded {DEADLINE_SECONDS}s")

        temp_dir = tempfile.TemporaryDirectory(prefix="nova-media-probe-", dir=output.parent)
        wav_path = Path(temp_dir.name) / "silent.wav"
        _write_silent_wav(wav_path)

        player = QtMultimedia.QMediaPlayer()
        player.setAudioOutput(None)
        state = _load_media(player, QtCore, QtMultimedia, wav_path, remaining_ms)
        report["media"] = {"status": state["status"], "error": state["error"], "timed_out": state["timed_out"], "source": "owned-silent-local-WAV", "audio_output": None, "play_called": False}
        if state["timed_out"]:
            raise TimeoutError(f"Qt media loading exceeded {DEADLINE_SECONDS}s")
        if state["error"] or state["status"] != "LoadedMedia":
            raise RuntimeError(f"Silent local WAV did not load: {state['status']} {state['error']}")
        if int((time.monotonic() - started) * 1000) >= deadline_ms:
            raise TimeoutError(f"Qt backend initialization exceeded {DEADLINE_SECONDS}s")

        frozen = bool(getattr(sys, "frozen", False))
        bundle_root = Path(getattr(sys, "_MEIPASS", output.parent)).resolve()
        if not frozen:
            # Explicit development-only fallback; never use this path in frozen mode.
            bundle_root = Path(QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.LibrariesPath)).resolve()
        ffmpeg_result, native_handles = _query_ffmpeg(bundle_root, frozen=frozen)
        libraries = ffmpeg_result.get("libraries", [])
        paths = [Path(str(item["path"])).resolve() for item in libraries]  # type: ignore[index]
        if not paths:
            raise RuntimeError("No bundled FFmpeg libraries could be loaded")
        discovered = {match.group(1).lower() for path in paths if (match := re.search(r"(?:lib)?(avutil|avcodec|avformat|swresample|swscale)", path.name, re.I))}
        if discovered != set(FFMPEG_LIBRARIES):
            raise RuntimeError(f"Incomplete FFmpeg runtime library set: {sorted(discovered)}")
        if any(item.get(field) is None for item in libraries for field in ("version", "configuration", "license")):  # type: ignore[union-attr]
            raise RuntimeError("Could not read version, build configuration, and license for every bundled FFmpeg library")
        if frozen and any(not path.is_relative_to(bundle_root) for path in paths):
            raise RuntimeError("An FFmpeg library resolved outside the frozen bundle root")
        if frozen:
            loaded_ffmpeg_paths = [
                Path(item).resolve()
                for item in ffmpeg_result.get("loaded_paths", [])
                if re.search(r"(avutil|avcodec|avformat|swresample|swscale)", str(item), re.I)
            ]
            if any(not path.is_relative_to(bundle_root) for path in loaded_ffmpeg_paths):
                raise RuntimeError("Qt loaded an FFmpeg library from outside the frozen bundle root")
        if any(item.get("lgpl_configuration") is False for item in libraries):  # type: ignore[union-attr]
            raise RuntimeError("Bundled FFmpeg configuration enables GPL or nonfree components")
        report["ffmpeg"] = ffmpeg_result
        report["success"] = True
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")  # type: ignore[union-attr]
        if ffmpeg_result:
            report["ffmpeg"] = ffmpeg_result
    finally:
        # Release owned Qt resources before deleting the owned sample file.
        if player is not None:
            try:
                from PySide6.QtCore import QUrl
                player.setSource(QUrl())
            except Exception:
                pass
            player = None
        if temp_dir is not None:
            temp_dir.cleanup()
        if qt_handler_installed:
            try:
                QtCore.qInstallMessageHandler(prior_qt_handler)
            except Exception:
                pass
        report["qt_backend_messages"] = qt_messages
        try:
            output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output_written = True
        except OSError as error:
            print(f"Could not write diagnostic JSON: {error}", file=sys.stderr)
        native_handles.clear()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["success"] and output_written else 1
