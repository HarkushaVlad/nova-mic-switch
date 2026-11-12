"""Frozen application entry point for PyInstaller."""

import sys


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--media-probe":
        from nova_mic_switch.media_probe import main
        raise SystemExit(main(sys.argv[2:]))
    else:
        from nova_mic_switch.desktop import main
    raise SystemExit(main())
