"""Frozen application entry point for PyInstaller."""

from nova_mic_switch.desktop import main


if __name__ == "__main__":
    raise SystemExit(main())
