# Preparing a release

## Manual CI build

The `Build release artifacts` workflow is `workflow_dispatch`-only. Run it
from the commit to build on native Ubuntu 22.04 x86_64 and Windows x64 runners.
Each platform syncs the locked Python 3.13 `dev` environment, runs the unit
suite with Qt's offscreen platform, and creates its native PyInstaller onedir
bundle. Linux then creates and safe-mode smoke-tests an AppImage; Windows
creates the per-user Inno Setup installer and runs a time-bounded safe-mode
smoke test on the frozen GUI. Both builds verify bundled license payloads and
excluded Qt components. CI does not install the application or edit startup
state on a developer host, or access microphone hardware during smoke tests.

Before either PyInstaller build, CI runs `scripts/collect_licenses.py
--fetch-python-license` with that platform's locked virtual environment. The
collector otherwise prefers a local Python license file; this explicit option
permits fetching only the exact CPython-version license text when the runner
does not provide it locally. The curated Qt license inputs remain offline and
hash-verified.

Ubuntu 22.04 (glibc 2.35) is the Linux build baseline. A successful build on a
newer local distribution is not evidence that the resulting AppImage runs on
older supported systems. Windows builds and frozen-executable smoke checks run
on the Windows CI runner; they are not validated by a Linux local build.

Build artifacts include:

- `NovaMicSwitch-VERSION-x86_64.AppImage`
- `NovaMicSwitch-Setup-VERSION.exe`
- Per-platform `SHA256SUMS` and a checksum file alongside each binary; a
  combined `SHA256SUMS` is attached to a draft release

The workflow uploads artifacts and does not publish automatically. The
AppImage tool and Inno Setup installer are downloaded only from exact approved
HTTPS GitHub release URLs and checked against `packaging/release-tools.json`.
Cached tools are hashed again before every use. The appimagetool URL is a
mutable `continuous` release, so its pinned digest must be deliberately
reviewed and updated when upstream changes the asset. Do not have automation
refresh that digest.

The AppImage runtime is pinned to the official `20251108` x86_64 asset with its
reviewed SHA-256, 944632-byte size, and source commit recorded in the strict
tool manifest. The runtime digest is never fetched or refreshed from an API.

## Optional draft release

The `publish` input defaults to false. A preflight job checks requests before
the platform builds: when publishing is requested it requires a `vVERSION` tag
that already exists in the complete checkout and points to the exact workflow
commit, plus an explicit `license_reviewed` acknowledgement covering notices
and real-platform testing. Invalid requests stop before the builds. The draft
job verifies each platform checksum, assembles one combined `SHA256SUMS`, and
creates a new draft with `gh release create --verify-tag --draft`. It refuses
to edit an existing release. The write-scoped GitHub token is exposed only to
the release-creation step; other jobs use read-only permissions and checkout
does not persist credentials.

The draft notes accurately state that CI did not test a real Nova HID device
or end-to-end microphone/audio routing. Review platform compatibility,
permissions, and third-party notices before publishing a draft to users.

## Local native builds

Build only on the target operating system; cross-compilation is unsupported.
Install Python 3.13 and the pinned uv version, then run `scripts/build_linux.sh`
on Linux or `scripts/build_windows.ps1` on Windows. The Windows script can
compile the installer when passed `-InnoSetupCompiler <path-to-ISCC.exe>`.
Release tools are downloaded to a project-local directory with
`python scripts/release_tools.py --tool appimagetool --output-dir build/release-tools`
or the corresponding `appimage-runtime` and `inno-setup` choices. The helper verifies the digest on
download and again for every cached-file use; remove a damaged cached file
explicitly before retrying. It does not install or execute downloaded tools.

After committing and pushing the reviewed source, a release owner must create
and push the intended version tag, then manually dispatch the workflow at that
tag's exact commit. This repository's tooling does not commit, push, tag, or
publish on the owner's behalf. Keep the publication input disabled for ordinary
builds.
