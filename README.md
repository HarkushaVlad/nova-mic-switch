# Nova Mic Switch

Use the button on your SteelSeries Arctis Nova 5 headset to switch between its microphone and your computer microphone.

## Install

Packaged releases will be available at [GitHub Releases](https://github.com/HarkushaVlad/nova-mic-switch/releases) when published. Windows users can run the published `NovaMicSwitch-Setup-<version>.exe`; Linux users can download the AppImage, enable **Allow executing file as program** in its file properties, and run it. Packaged apps do not require Python.

Until a package is available, install Python 3.10+ and [uv](https://docs.astral.sh/uv/), then run in the project folder:

```sh
uv sync --locked --no-dev
uv run --no-dev nova-mic-gui
```

Linux requires `pactl` and permission to access the headset HID device. See [Linux setup](docs/distribution.md#linux); do not run the app as root.

## Use

Choose your computer and headset microphones, select a button action (three short **POWER** presses by default), then click **Start listening** and confirm. Set Discord's input to **Default**. Beeps indicate a successful switch when feedback is enabled.

Enable **Start Nova Mic Switch when I sign in** to start at login; minimize to the tray if available. Windows switching changes the default microphone for other apps; Linux can switch for Discord only or system-wide.

Use short POWER presses: holding the button can turn off the headset.
