from pathlib import Path
import importlib.util
import json
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


ROOT = Path(SPECPATH).resolve().parent
PACKAGE = ROOT / "src"
LICENSE_COLLECTOR = ROOT / "scripts" / "collect_licenses.py"
license_collector = sys.modules.get("nova_license_collector")
if license_collector is None:
    collector_spec = importlib.util.spec_from_file_location("nova_license_collector", LICENSE_COLLECTOR)
    license_collector = importlib.util.module_from_spec(collector_spec)
    collector_spec.loader.exec_module(license_collector)
LICENSES = license_collector.ensure_licenses(ROOT / "build" / "licenses")
license_manifest = json.loads((LICENSES / "manifest.json").read_text(encoding="utf-8"))
license_data_paths = [LICENSES / entry["destination"] for entry in license_manifest["texts"]]
license_data_paths.append(LICENSES / "THIRD-PARTY-NOTICES.md")
license_data_paths.append(LICENSES / "manifest.json")
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
if not (IS_WINDOWS or IS_LINUX):
    raise SystemExit("Nova Mic Switch packaging supports Windows and Linux only")

hiddenimports = [
    "nova_mic_switch.audio_linux",
    "nova_mic_switch.audio_windows",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "shiboken6",
]
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
]
if IS_WINDOWS:
    hiddenimports.append("hid")
else:
    hiddenimports.append("hidraw")

a = Analysis(
    [str(ROOT / "packaging" / "frozen_entry.py")],
    pathex=[str(PACKAGE)],
    binaries=[],
    datas=(
        collect_data_files("PySide6", includes=["translations/qtbase_*.qm"])
        + [(str(path), str(Path("licenses") / path.relative_to(LICENSES).parent)) for path in license_data_paths]
        + [
            item
            for distribution in (
                "PySide6",
                "PySide6_Essentials",
                "PySide6_Addons",
                "shiboken6",
                "hidapi",
                "pyinstaller",
            )
            for item in copy_metadata(distribution)
        ]
    ),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)


def _excluded_unused_qt_plugin(row):
    """Drop unused Qt Virtual Keyboard and Qt PDF binaries/data from either TOC."""
    destination, source = str(row[0]).replace("\\", "/"), str(row[1]).replace("\\", "/")
    components = f"{destination}/{source}".lower().split("/")
    if any("virtualkeyboard" in component for component in components):
        return True
    for component in components:
        basename = component.rsplit("/", 1)[-1]
        if basename in ("libqpdf.so", "qpdf.dll"):
            return True
        if basename.startswith("libqt6pdf") or basename.startswith("qt6pdf"):
            return True
    return False


# Keep ordinary platform input, image/SVG, multimedia/FFmpeg, and any QtQuick closure.
a.binaries = [row for row in a.binaries if not _excluded_unused_qt_plugin(row)]
a.datas = [row for row in a.datas if not _excluded_unused_qt_plugin(row)]
if IS_LINUX:
    sys.path.insert(0, str(ROOT / "scripts"))
    from collect_linux_notices import collect_linux_notices

    linux_notice_result = collect_linux_notices(
        toc_rows=a.binaries + a.datas,
        output_dir=ROOT / "build" / "linux-system-notices",
    )
    manifest_path = Path(linux_notice_result["manifest"])

    def _linux_notice_path_has_symlink(path):
        if not path.is_absolute() or any(part == ".." for part in path.parts):
            return True
        cursor = Path(path.anchor)
        for part in path.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                return True
        return False

    if manifest_path.name != "manifest.json" or _linux_notice_path_has_symlink(manifest_path):
        raise SystemExit("Linux notice collector returned an invalid manifest path")
    notice_root = manifest_path.parent.resolve(strict=True)
    generated_files = linux_notice_result["files"]
    if not isinstance(generated_files, list):
        raise SystemExit("Linux notice collector returned an invalid generated-file list")
    notice_data_rows = []
    notice_relpaths = set()
    required_notice_files = {"manifest.json", "THIRD-PARTY-NOTICES.md"}
    for generated_file in generated_files:
        path = Path(generated_file)
        if _linux_notice_path_has_symlink(path):
            raise SystemExit("Linux notice collector returned an unsafe generated-file path")
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(notice_root)
        except (OSError, ValueError) as exc:
            raise SystemExit("Linux notice output is missing or escapes its output root") from exc
        if not resolved.is_file() or resolved.suffix.lower() not in {".txt", ".json", ".md"}:
            raise SystemExit("Linux notice collector returned a non-text output")
        relative_name = relative.as_posix()
        if relative_name in notice_relpaths:
            raise SystemExit("Linux notice collector returned duplicate output paths")
        notice_relpaths.add(relative_name)
        if relative_name not in required_notice_files and not (
            len(relative.parts) == 3 and relative.parts[:2] == ("notices", "package")
            and resolved.suffix.lower() == ".txt"
        ):
            raise SystemExit("Linux notice collector returned an unexpected output path")
        if relative_name in required_notice_files and resolved.stat().st_size == 0:
            raise SystemExit("Linux notice collector returned an empty index or manifest")
        notice_destination = (Path("licenses") / "linux-system" / relative).as_posix()
        notice_data_rows.append((notice_destination, str(resolved), "DATA"))
    if not required_notice_files <= notice_relpaths:
        raise SystemExit("Linux notice collector omitted its manifest or notice index")
    a.datas.extend(notice_data_rows)
if IS_WINDOWS and os.name == "nt":
    sys.path.insert(0, str(ROOT / "packaging" / "windows"))
    from native_policy import apply_windows_native_policy

    apply_windows_native_policy(a, ROOT, ROOT / ".venv", Path(sys.base_prefix))

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NovaMicSwitch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    **({"icon": str(ROOT / "packaging" / "icons" / "nova-mic-switch.ico")} if IS_WINDOWS else {}),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NovaMicSwitch",
)
