"""Explicit per-user login startup integration for supported desktop platforms."""

from __future__ import annotations

import os
import ntpath
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE_NAME = "NovaMicSwitch"
LINUX_FILENAME = "nova-mic-switch.desktop"
_LINUX_MARKER = "X-NovaMicSwitch-Owned=true"
_LINUX_NAME = "Name=NovaMicSwitch"


class StartupError(RuntimeError):
    """An actionable error while reading or changing login startup state."""


def _absolute_path(path: Path, platform: str) -> str:
    if platform == "win32":
        import ntpath

        return ntpath.abspath(str(path))
    return str(path.expanduser().absolute())


def default_startup_command(config_dir: Path | None = None) -> tuple[str, ...]:
    """Return an appropriate launch command without changing system state."""
    platform = sys.platform
    executable = sys.executable
    if getattr(sys, "frozen", False):
        if platform.startswith("linux") and "APPIMAGE" in os.environ:
            appimage = os.environ["APPIMAGE"]
            if not appimage:
                raise StartupError("The APPIMAGE environment variable is empty; login startup needs the original AppImage path.")
            if any(ord(char) < 32 or ord(char) == 127 for char in appimage):
                raise StartupError("The APPIMAGE path contains control characters and cannot be used for login startup.")
            if not os.path.isabs(appimage):
                raise StartupError("The APPIMAGE path must be absolute to configure login startup.")
            if not Path(appimage).is_file():
                raise StartupError(f"The APPIMAGE path is not an existing regular file: {appimage}")
            command = (appimage, "--appimage-extract-and-run")
        else:
            command = (_absolute_path(Path(executable), platform),)
    else:
        if platform == "win32":
            candidate = Path(executable).with_name("pythonw.exe")
            if candidate.is_file():
                executable = str(candidate)
        command = (_absolute_path(Path(executable), platform), "-m", "nova_mic_switch.desktop")
    if config_dir is not None:
        command += ("--config-dir", _absolute_path(Path(config_dir), platform))
    return command


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)):
        raise StartupError("Startup command must be a sequence of arguments, not a string.")
    if not isinstance(command, Sequence):
        raise StartupError("Startup command must be a sequence of arguments.")
    args = tuple(command)
    if not args:
        raise StartupError("Startup command must contain an executable.")
    if any(not isinstance(arg, str) or not arg for arg in args):
        raise StartupError("Startup command arguments must be non-empty strings.")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in arg) for arg in args):
        raise StartupError("Startup command cannot contain control characters.")
    return args


def _desktop_escape_argument(argument: str) -> str:
    # Exec quoting is distinct from shell quoting. Then escape backslashes for
    # the desktop-entry string value layer, and percent signs per the Exec spec.
    quoted = "".join({"\\": "\\\\", '"': '\\"', "`": "\\`", "$": "\\$"}.get(char, char)
                     for char in argument)
    escaped = '"' + quoted + '"'
    return escaped.replace("\\", "\\\\").replace("%", "%%")


def _desktop_exec(command: Sequence[str]) -> str:
    if "=" in command[0]:
        raise StartupError("The startup executable cannot contain '=' in a desktop entry.")
    return " ".join(_desktop_escape_argument(arg) for arg in command)


class StartupManager:
    """Manage only this application's per-user login startup entry."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        platform: str | None = None,
        config_home: Path | None = None,
        registry=None,
        environ: Mapping[str, str] | None = None,
    ):
        self.platform = sys.platform if platform is None else platform
        self.command = _validate_command(command)
        if self.platform == "win32":
            if not ntpath.isabs(self.command[0]):
                raise StartupError("The startup executable must be an absolute Windows path.")
        elif self.platform == "linux" and not os.path.isabs(self.command[0]):
            raise StartupError("The startup executable must be an absolute path.")
        env = os.environ if environ is None else environ
        home = config_home if config_home is not None else Path(
            env.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        )
        self.config_home = Path(home).expanduser()
        self._registry = registry

    @property
    def available(self) -> bool:
        return self.platform in ("win32", "linux")

    def is_enabled(self) -> bool:
        self._require_available()
        if self.platform == "win32":
            registry = self._get_registry()
            try:
                key = registry.OpenKey(registry.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, registry.KEY_READ)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise StartupError(f"Could not read the Nova Mic Switch login startup entry: {exc}") from exc
            try:
                try:
                    value, value_type = registry.QueryValueEx(key, WINDOWS_VALUE_NAME)
                except FileNotFoundError:
                    return False
                except OSError as exc:
                    raise StartupError(f"Could not read the Nova Mic Switch login startup value: {exc}") from exc
                return value_type == registry.REG_SZ and isinstance(value, str) and bool(value)
            finally:
                registry.CloseKey(key)
        path = self._linux_path
        if path.is_symlink():
            raise StartupError(f"Refusing to inspect a symlink at the startup entry: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except (OSError, UnicodeError) as exc:
            raise StartupError(f"Could not read the Nova Mic Switch startup entry at {path}: {exc}") from exc
        values = self._parse_owned_entry(text, path)
        return bool(values.get("Exec")) and values.get("Hidden") != "true"

    def set_enabled(self, enabled: bool, *, start_minimized: bool = True) -> None:
        if type(enabled) is not bool or type(start_minimized) is not bool:
            raise TypeError("enabled and start_minimized must be booleans")
        self._require_available()
        if self.platform == "win32":
            self._set_windows(enabled, start_minimized)
        else:
            self._set_linux(enabled, start_minimized)

    def _require_available(self) -> None:
        if not self.available:
            raise StartupError(f"Login startup is not supported on platform {self.platform!r}.")

    @property
    def _linux_path(self) -> Path:
        return self.config_home / "autostart" / LINUX_FILENAME

    def _get_registry(self):
        if self._registry is None:
            if self.platform != "win32":
                raise StartupError("The Windows registry is unavailable on this platform.")
            try:
                import winreg
            except ImportError as exc:
                raise StartupError("Windows registry support is unavailable in this Python runtime.") from exc
            self._registry = winreg
        return self._registry

    def _command_for_minimized(self, start_minimized: bool) -> tuple[str, ...]:
        return self.command + (("--tray",) if start_minimized else ())

    def _windows_command(self, start_minimized: bool) -> str:
        return subprocess.list2cmdline(list(self._command_for_minimized(start_minimized)))

    def _set_windows(self, enabled: bool, start_minimized: bool) -> None:
        registry = self._get_registry()
        if enabled:
            value = self._windows_command(start_minimized)
            if len(value) > 260:
                raise StartupError("The Windows login startup command exceeds the 260-character Run value limit.")
            try:
                key = registry.OpenKey(registry.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, registry.KEY_SET_VALUE)
            except FileNotFoundError:
                try:
                    key = registry.CreateKeyEx(
                        registry.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, registry.KEY_SET_VALUE
                    )
                except OSError as exc:
                    raise StartupError(f"Could not create the current-user startup registry key: {exc}") from exc
            except OSError as exc:
                raise StartupError(f"Could not open the current-user startup registry key for writing: {exc}") from exc
            try:
                registry.SetValueEx(key, WINDOWS_VALUE_NAME, 0, registry.REG_SZ, value)
            except OSError as exc:
                raise StartupError(f"Could not enable Nova Mic Switch at login: {exc}") from exc
            finally:
                registry.CloseKey(key)
            try:
                key = registry.OpenKey(registry.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, registry.KEY_READ)
                try:
                    actual, value_type = registry.QueryValueEx(key, WINDOWS_VALUE_NAME)
                finally:
                    registry.CloseKey(key)
            except OSError as exc:
                raise StartupError(f"Startup value was written but could not be verified: {exc}") from exc
            if actual != value or value_type != registry.REG_SZ:
                raise StartupError("Startup value was written but does not match the requested command.")
        else:
            try:
                key = registry.OpenKey(registry.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, registry.KEY_SET_VALUE)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StartupError(f"Could not open the current-user startup registry key: {exc}") from exc
            try:
                try:
                    registry.DeleteValue(key, WINDOWS_VALUE_NAME)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    raise StartupError(f"Could not disable Nova Mic Switch at login: {exc}") from exc
            finally:
                registry.CloseKey(key)
            try:
                key = registry.OpenKey(registry.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, registry.KEY_READ)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StartupError(f"Could not verify removal of the Nova Mic Switch startup value: {exc}") from exc
            try:
                try:
                    registry.QueryValueEx(key, WINDOWS_VALUE_NAME)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    raise StartupError(f"Could not verify removal of the Nova Mic Switch startup value: {exc}") from exc
                raise StartupError("Nova Mic Switch startup value remains after removal.")
            finally:
                registry.CloseKey(key)

    def _linux_content(self, start_minimized: bool) -> str:
        exec_command = _desktop_exec(self._command_for_minimized(start_minimized))
        return "\n".join((
            "[Desktop Entry]", "Type=Application", _LINUX_NAME,
            "Terminal=false", f"Exec={exec_command}", _LINUX_MARKER, "",
        ))

    def _parse_owned_entry(self, text: str, path: Path) -> dict[str, str]:
        lines = text.splitlines()
        if _LINUX_MARKER not in lines:
            raise StartupError(f"A non-owned file already occupies the startup entry path: {path}")
        if "[Desktop Entry]" not in lines or "Type=Application" not in lines:
            raise StartupError(f"The owned startup entry is malformed: {path}")
        values = {}
        for line in lines:
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        if values.get("Name") != "NovaMicSwitch" or values.get("Terminal") != "false":
            raise StartupError(f"The owned startup entry is malformed: {path}")
        if not values.get("Exec"):
            raise StartupError(f"The owned startup entry has no executable command: {path}")
        return values

    def _set_linux(self, enabled: bool, start_minimized: bool) -> None:
        path = self._linux_path
        if path.is_symlink():
            raise StartupError(f"Refusing to replace or remove a symlink at the startup entry: {path}")
        if enabled:
            content = self._linux_content(start_minimized)
            if path.exists():
                try:
                    self._parse_owned_entry(path.read_text(encoding="utf-8"), path)
                except StartupError:
                    raise
                except (OSError, UnicodeError) as exc:
                    raise StartupError(f"Could not inspect the existing startup entry at {path}: {exc}") from exc
            parent = path.parent
            temporary = None
            try:
                parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=parent,
                                                 prefix=f".{LINUX_FILENAME}.", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                temporary = None
                try:
                    directory_fd = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            except (OSError, StartupError) as exc:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                if isinstance(exc, StartupError):
                    raise
                raise StartupError(f"Could not enable Nova Mic Switch at login in {path}: {exc}") from exc
            try:
                written = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise StartupError(f"Startup entry was written but could not be verified: {exc}") from exc
            if written != content:
                raise StartupError("Startup entry was written but does not match the requested content.")
        else:
            if not path.exists():
                return
            try:
                self._parse_owned_entry(path.read_text(encoding="utf-8"), path)
                path.unlink()
            except StartupError:
                raise
            except (OSError, UnicodeError) as exc:
                raise StartupError(f"Could not disable Nova Mic Switch at login in {path}: {exc}") from exc
            if path.exists() or path.is_symlink():
                raise StartupError("Startup entry remains after removal.")
