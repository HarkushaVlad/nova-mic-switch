#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
BUNDLE="$ROOT/dist/linux/NovaMicSwitch"
OUTPUT_DIR="$ROOT/dist/linux"
WORK_DIR="${APPIMAGE_WORK_DIR:-$ROOT/build/appimage}"
ICON="$ROOT/packaging/icons/nova-mic-switch.svg"
ICON_PNG="$ROOT/packaging/icons/nova-mic-switch.png"
APPIMAGETOOL="${APPIMAGETOOL:-}"
RUNTIME_FILE="${APPIMAGE_RUNTIME_FILE:-}"

usage() {
    cat <<'EOF'
Usage: build_appimage.sh --appimagetool /absolute/path/to/appimagetool --runtime-file /absolute/path/to/runtime [options]

Build a native x86_64 AppImage from an existing Linux onedir bundle. No tools
or runtime files are downloaded. Both the appimagetool executable and its ELF
runtime must be supplied explicitly from trusted, verified sources.

Options:
  --appimagetool PATH  appimagetool executable (or APPIMAGETOOL)
  --runtime-file PATH  verified AppImage ELF runtime (or APPIMAGE_RUNTIME_FILE)
  --bundle PATH        onedir bundle (default: dist/linux/NovaMicSwitch)
  --output-dir PATH    artifact directory (default: dist/linux)
  --work-dir PATH      POSIX-permission-capable AppDir staging root (or APPIMAGE_WORK_DIR)
  --icon PATH          SVG icon (default: packaging/icons/nova-mic-switch.svg)
  -h, --help           show this help
EOF
}

die() {
    printf 'build_appimage: %s\n' "$*" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --appimagetool)
            (($# >= 2)) || die '--appimagetool requires a path'
            APPIMAGETOOL=$2
            shift 2
            ;;
        --runtime-file)
            (($# >= 2)) || die '--runtime-file requires a path'
            RUNTIME_FILE=$2
            shift 2
            ;;
        --bundle)
            (($# >= 2)) || die '--bundle requires a path'
            BUNDLE=$2
            shift 2
            ;;
        --output-dir)
            (($# >= 2)) || die '--output-dir requires a path'
            OUTPUT_DIR=$2
            shift 2
            ;;
        --work-dir)
            (($# >= 2)) || die '--work-dir requires a path'
            WORK_DIR=$2
            shift 2
            ;;
        --icon)
            (($# >= 2)) || die '--icon requires a path'
            ICON=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ "$(uname -s)" == Linux ]] || die 'must run on native Linux; cross-compilation is not supported'
[[ "$(uname -m)" == x86_64 ]] || die 'only native x86_64 builds are supported'

[[ -n "$APPIMAGETOOL" ]] || die 'provide --appimagetool or APPIMAGETOOL'
[[ "$APPIMAGETOOL" == /* ]] || die 'appimagetool path must be absolute'
[[ -f "$APPIMAGETOOL" && -x "$APPIMAGETOOL" ]] || die "appimagetool is not an executable file: $APPIMAGETOOL"
[[ -n "$RUNTIME_FILE" ]] || die 'provide --runtime-file or APPIMAGE_RUNTIME_FILE with a verified runtime'
[[ "$RUNTIME_FILE" == /* ]] || die 'runtime file path must be absolute'
[[ -f "$RUNTIME_FILE" ]] || die "runtime file does not exist: $RUNTIME_FILE"

[[ -d "$BUNDLE" ]] || die "onedir bundle does not exist: $BUNDLE"
[[ -x "$BUNDLE/NovaMicSwitch" ]] || die "bundle executable is missing or not executable: $BUNDLE/NovaMicSwitch"
[[ -d "$BUNDLE/_internal" ]] || die "bundle contents are incomplete (missing _internal): $BUNDLE"
[[ -f "$ICON" ]] || die "SVG icon is missing: $ICON"
[[ -f "$ICON_PNG" ]] || die "PNG directory icon is missing: $ICON_PNG"
[[ -f "$ROOT/packaging/linux/AppRun" ]] || die 'packaging/linux/AppRun is missing'
[[ -f "$ROOT/packaging/linux/nova-mic-switch.desktop" ]] || die 'packaging/linux/nova-mic-switch.desktop is missing'
command -v sha256sum >/dev/null 2>&1 || die 'sha256sum is required'
command -v stat >/dev/null 2>&1 || die 'stat is required to validate AppDir permissions'
[[ -n "$WORK_DIR" ]] || die 'work directory path must not be empty'

BUNDLE="$(cd -- "$BUNDLE" && pwd -P)"
ICON="$(cd -- "$(dirname -- "$ICON")" && printf '%s/%s\n' "$PWD" "$(basename -- "$ICON")")"
OUTPUT_DIR="$(mkdir -p -- "$OUTPUT_DIR" && cd -- "$OUTPUT_DIR" && pwd -P)"
WORK_DIR="$(mkdir -p -- "$WORK_DIR" && cd -- "$WORK_DIR" && pwd -P)"

PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "project Python environment is missing: $PYTHON"
PROJECT_VERSION=$("$PYTHON" - "$ROOT/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as project_file:
    print(tomllib.load(project_file)["project"]["version"])
PY
) || die 'could not read the project version from pyproject.toml'
[[ "$PROJECT_VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+-]*$ ]] || die "unsafe project version for artifact filename: $PROJECT_VERSION"

APPIMAGE_NAME="NovaMicSwitch-${PROJECT_VERSION}-x86_64.AppImage"
APPIMAGE_PATH="$OUTPUT_DIR/$APPIMAGE_NAME"
CHECKSUM_PATH="$APPIMAGE_PATH.sha256"

APPDIR_STAGE=$(mktemp -d "$WORK_DIR/stage.XXXXXX")
ARTIFACT_STAGE=""
cleanup() {
    rm -rf -- "$APPDIR_STAGE"
    if [[ -n "$ARTIFACT_STAGE" ]]; then
        rm -rf -- "$ARTIFACT_STAGE"
    fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
ARTIFACT_STAGE=$(mktemp -d "$OUTPUT_DIR/.appimage-stage.XXXXXX")

APPDIR="$APPDIR_STAGE/NovaMicSwitch.AppDir"
mkdir -p -- "$APPDIR/usr/lib/nova-mic-switch"
cp -a -- "$BUNDLE/." "$APPDIR/usr/lib/nova-mic-switch/"
install -m 0755 -- "$ROOT/packaging/linux/AppRun" "$APPDIR/AppRun"
install -m 0644 -- "$ROOT/packaging/linux/nova-mic-switch.desktop" "$APPDIR/nova-mic-switch.desktop"
install -m 0644 -- "$ICON" "$APPDIR/nova-mic-switch.svg"
install -m 0644 -- "$ICON_PNG" "$APPDIR/nova-mic-switch.png"
ln -s -- nova-mic-switch.png "$APPDIR/.DirIcon"
# PyInstaller can preserve restrictive build-user modes; normalize the staged
# payload and fail clearly if the staging filesystem cannot represent POSIX modes.
chmod -R a+rX -- "$APPDIR"
APPDIR_MODE=$(stat -c '%a' -- "$APPDIR")
APPRUN_MODE=$(stat -c '%a' -- "$APPDIR/AppRun")
if [[ "$APPDIR_MODE" != 755 || "$APPRUN_MODE" != 755 ]]; then
    die "AppDir staging filesystem did not preserve required POSIX modes (AppDir=$APPDIR_MODE, AppRun=$APPRUN_MODE); use --work-dir on a POSIX-permission-capable filesystem"
fi

TEMP_APPIMAGE="$ARTIFACT_STAGE/$APPIMAGE_NAME"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" --runtime-file "$RUNTIME_FILE" "$APPDIR" "$TEMP_APPIMAGE"
[[ -s "$TEMP_APPIMAGE" ]] || die 'appimagetool did not create a nonempty AppImage'

mv -f -- "$TEMP_APPIMAGE" "$APPIMAGE_PATH"
(cd -- "$OUTPUT_DIR" && sha256sum -- "$APPIMAGE_NAME" > "$APPIMAGE_NAME.sha256")
printf 'AppImage created: %s\n' "$APPIMAGE_PATH"
printf 'SHA-256 file: %s\n' "$CHECKSUM_PATH"
