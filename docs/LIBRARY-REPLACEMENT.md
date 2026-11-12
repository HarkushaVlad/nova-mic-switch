# Replacing bundled libraries for local testing

Nova Mic Switch's own utility code is MIT-licensed; this guide covers practical local testing of compatible, dynamically linked Qt or FFmpeg replacements. It does not promise arbitrary replacements will work or claim complete third-party license coverage.

## Before you start

- Use libraries compatible with the package's OS and architecture, Qt major version and ABI, Qt plugins, FFmpeg SONAMEs/interfaces, and the other libraries they depend on. Replace a coordinated Qt library/plugin group from the same compatible build; do not assume replacing one file is sufficient.
- Stop Nova Mic Switch completely before testing or replacing files. Closing its window may leave it running in the system tray. A second launch can activate the existing process, which still has the old libraries loaded.
- Keep an untouched package or test copy for recovery. Updates or reinstalling may overwrite modified installed files.
- This is an at-your-own-risk local test; no ABI compatibility or successful operation is guaranteed.

## Linux AppImage

The AppImage's `AppRun` launches `usr/lib/nova-mic-switch/NovaMicSwitch`. Extract the original AppImage once into a persistent, user-writable directory on a POSIX-permission-capable filesystem; do not test from a temporary self-extract mount.

```sh
mkdir -p "$HOME/nova-mic-switch-library-test"
cd "$HOME/nova-mic-switch-library-test"
/absolute/path/to/original-NovaMicSwitch.AppImage --appimage-extract
```

The extracted tree is `squashfs-root/`. The application and its PyInstaller `_internal` directory are under `squashfs-root/usr/lib/nova-mic-switch/`; keep the tree and its directory structure intact. Discover the Qt core file and plugin locations in this package rather than assuming a Qt installation layout:

```sh
find squashfs-root/usr/lib/nova-mic-switch/_internal -type f \
  \( -name 'libQt6Core.so*' -o -name 'libavcodec.so*' -o -name 'libavformat.so*' \) -print
```

Replace the relevant library files and, when needed, their matching plugins **inside this extracted `_internal` tree**. Preserve the original files elsewhere if you may need to restore them. No AppImage repack, signing, or encryption step is required: run the extracted app through its `AppRun` wrapper so it uses the extracted AppDir layout.

```sh
./squashfs-root/AppRun --safe-mode \
  --config-dir "$HOME/.cache/nova-mic-switch-library-test" --smoke-test
```

The safe-mode smoke test checks application startup with an isolated config location. It does not test hardware, audio capture/playback, codecs in functional use, or Discord integration.

## Windows per-user installation

The installer is per-user and its configured default directory is `%LOCALAPPDATA%\Programs\Nova Mic Switch` (`{localappdata}\Programs\Nova Mic Switch` in Inno Setup). Stop the tray process before changing or testing any installed library.

Use PowerShell to inspect the installed bundle and locate the Qt core and FFmpeg DLLs instead of assuming their exact subdirectory:

```powershell
$Install = Join-Path $env:LOCALAPPDATA 'Programs\Nova Mic Switch'
Get-ChildItem -LiteralPath (Join-Path $Install '_internal') -File -Recurse |
  Where-Object { $_.Name -like 'Qt6Core.dll' -or $_.Name -like 'avcodec*.dll' -or $_.Name -like 'avformat*.dll' } |
  Select-Object -ExpandProperty FullName
```

Replace compatible files and their matching plugins under the installed `_internal` directory. Alternatively, copy `NovaMicSwitch.exe` and the **whole** `_internal` directory to a user-writable test folder, then make replacements in that copy; the installer does not need to be rebuilt. Do not disable operating-system protections or elevate permissions to make the test work.

```powershell
$Test = Join-Path $env:LOCALAPPDATA 'NovaMicSwitch-library-test'
& (Join-Path $Test 'NovaMicSwitch.exe') --safe-mode `
  --config-dir (Join-Path $env:TEMP 'NovaMicSwitch-library-test-config') --smoke-test
```

As on Linux, this smoke test covers startup only, not device access, audio, codec functionality, or Discord integration.

## Notices, sources, and permissions

- Keep the package's bundled license texts, third-party notices, and source/package metadata intact. A modified-library test does not itself provide source or notices that a library's license may require.
- When release companions are published, consult their `SOURCE-INDEX.md`, `source-index.json`, and `SHA256SUMS` for corresponding source and build information. This repository's upstream manifest is [packaging/sources/upstream.json](../packaging/sources/upstream.json). A link or filename here does not mean release assets are currently published or downloadable.
- Third-party license grants and conditions still apply. Users may replace compatible LGPL-covered libraries and reverse engineer them to debug their modifications; this guide adds no restriction on that activity. It is not a guarantee of complete third-party license compliance.
