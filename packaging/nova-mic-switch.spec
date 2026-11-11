from pathlib import Path
import importlib.util
import json
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
