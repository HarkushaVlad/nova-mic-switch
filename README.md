# Nova Mic Switch

Switch between the microphones on your SteelSeries Arctis Nova 5 headset and computer using the headset's POWER button.

## Install

Download the Windows x64 installer or Linux x86_64 AppImage from [GitHub Releases](https://github.com/HarkushaVlad/nova-mic-switch/releases). Run the installer, or make the AppImage executable and launch it. Packaged apps do not need Python.

Linux requires `pactl`. If the app reports `cannot open media HID interface`, enable access to the supported Nova USB dongle (`1038:2232`) once on a systemd desktop:

```sh
sudo tee /etc/udev/rules.d/70-nova-mic-switch.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1038", ATTRS{idProduct}=="2232", TAG+="uaccess"
EOF
sudo chmod 0644 /etc/udev/rules.d/70-nova-mic-switch.rules
sudo udevadm control --reload-rules
```

Unplug and reconnect the **USB dongle** afterward. Do not run the AppImage with `sudo`. The [rule](packaging/linux/99-nova-5p-hidraw.rules.example) grants access only to this dongle for the active local user.

## Use

Choose the computer and headset microphones, click **Start listening**, and confirm. Set Discord's input to **Default**.

By default, press POWER three times briefly to switch microphones; do not hold the button. Optional feedback plays a short beep after a switch.
