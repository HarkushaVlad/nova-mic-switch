# Nova Mic Switch

Switch between the microphones on your SteelSeries Arctis Nova 5 headset and computer using the headset's POWER button.

## Install

Download the Windows x64 installer or Linux x86_64 AppImage from [GitHub Releases](https://github.com/HarkushaVlad/nova-mic-switch/releases). Run the installer, or make the AppImage executable and launch it. Packaged apps do not need Python.

Linux requires `pactl` and headset HID permissions. Review the optional [udev rule](packaging/linux/99-nova-5p-hidraw.rules.example); do not run the app as root.

## Use

Choose the computer and headset microphones, click **Start listening**, and confirm. Set Discord's input to **Default**.

By default, press POWER three times briefly to switch microphones; do not hold the button. Optional feedback plays a short beep after a switch.
