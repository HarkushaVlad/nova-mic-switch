#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$(uname -s)" != Linux ]]; then
    printf 'This script must run on Linux; cross-compilation is not supported.\n' >&2
    exit 2
fi
if [[ "${1:-}" == "--help" ]]; then
    printf 'Build the Linux onedir bundle at %s/dist/linux/NovaMicSwitch (no install or autostart changes).\n' "$ROOT"
    exit 0
fi
if [[ $# -ne 0 ]]; then
    printf 'Usage: %s [--help]\n' "$0" >&2
    exit 2
fi

export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$ROOT/build/pyinstaller-cache}"
cd "$ROOT"
uv sync --locked --group dev
"$ROOT/.venv/bin/pyinstaller" --noconfirm --clean \
    --distpath "$ROOT/dist/linux" --workpath "$ROOT/build/linux" \
    "$ROOT/packaging/nova-mic-switch.spec"
printf 'Bundle created at %s/dist/linux/NovaMicSwitch\n' "$ROOT"
