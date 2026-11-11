# Installing and running Nova Mic Switch

Packaged apps include the Python runtime and application dependencies. They still require the operating-system audio and device interfaces described below. Release links will be posted on [GitHub Releases](https://github.com/HarkushaVlad/nova-mic-switch/releases) when packages are published.

## Windows

When a Windows installer is published, download `NovaMicSwitch-Setup-<version>.exe` from GitHub Releases and follow its prompts. Otherwise, run the published portable app if one is provided. Packaged Windows builds must be made on native Windows. This environment's Windows build and hardware behavior have not been verified; see [Windows audio status](windows-audio.md).

Windows switching changes the system-default microphone and can affect other apps, not only Discord. The application does not need to run as administrator.

## Linux

The AppImage is the primary packaged option. Download it, enable **Allow executing file as program** in its file properties, then run it. Alternatively:

```sh
chmod +x NovaMicSwitch-0.1.0-x86_64.AppImage
./NovaMicSwitch-0.1.0-x86_64.AppImage
```

If the system cannot run AppImages through FUSE, use the runtime's extraction fallback:

```sh
./NovaMicSwitch-0.1.0-x86_64.AppImage --appimage-extract-and-run
```

Keep the AppImage at a fixed location if using **Start Nova Mic Switch when I sign in**. Login startup uses the desktop environment's XDG autostart support; behavior can vary by environment. Tray support also varies, and the app falls back to a visible window if no tray is available.

An onedir package is another option: keep the complete `NovaMicSwitch` directory together and run its `NovaMicSwitch` executable. No Python installation is required. The Linux AppImage built locally was tested on Arch Linux with glibc 2.44; this does not establish compatibility with Ubuntu 22.04 or older/unrelated distributions. Configured Ubuntu and Windows CI/package results have not been verified here.

Linux routing requires `pactl` from PulseAudio or PipeWire-Pulse. Install/configure that system component separately if unavailable. Nova headset HID access depends on host device permissions. Resolve access through your distribution's documented policy; do not run the app as root. [`99-nova-5p-hidraw.rules.example`](99-nova-5p-hidraw.rules.example) is an optional example, not installed or enabled by the app. Review it and your distribution's policy before manually installing a rule.

## Building packages (maintainers)

For the AppImage build steps and verified tool requirements, see [releasing](releasing.md). Builds use the locked development environment and repository-local `.uv-cache` by default; an incomplete cache may require network access.

```sh
# Native Linux only; produces dist/linux/NovaMicSwitch/
./scripts/build_linux.sh
```

```powershell
# Native Windows only; produces dist\windows\NovaMicSwitch\
.\scripts\build_windows.ps1

# Optional: use an already-installed Inno Setup 6 compiler.
.\scripts\build_windows.ps1 -InnoSetupCompiler 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

Windows cross-compilation is not supported. Build scripts do not install Inno Setup or change system configuration, permissions, startup settings, or user settings. Do not present an untested package as a release.

## Third-party notices and license obligations

The current Linux bundle includes 18 curated, nonempty third-party license texts, a manifest with hashes, and `THIRD-PARTY-NOTICES.md`. These materials are not a complete third-party SBOM or a legal review. Exact Windows-build contents, required attributions, corresponding source and relinking obligations still need review for each release. Qt components may be offered under LGPL, GPL, or commercial terms depending on the component and distribution; verify the obligations for the exact package. This repository does not assign a public license to Nova Mic Switch; do not infer one from third-party terms.
