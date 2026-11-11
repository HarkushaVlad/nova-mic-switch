# Login startup

Nova Mic Switch does not register itself at login merely by importing the
application or opening its settings. The desktop preference must explicitly
create or remove the current user's startup entry. The reported state reflects
whether its own registration entry is present and well-formed; it does not
prove that the operating system or desktop session will execute it.

On Windows, the startup integration uses only the current user's
`Software\\Microsoft\\Windows\\CurrentVersion\\Run` value named
`NovaMicSwitch`. A nonempty `REG_SZ` value counts as registered, including an
entry written by an installer or an older app version. It launches the
application hidden in the system tray when the minimized option is selected.
The integration creates the Run key only when enabling startup and never
removes the key or changes other values. The Windows installer may
enable this option by default as an installation action.

On Linux, startup is represented by the per-user
`$XDG_CONFIG_HOME/autostart/nova-mic-switch.desktop` file (or
`~/.config/autostart/nova-mic-switch.desktop` when `XDG_CONFIG_HOME` is not
set). This is a graphical-session autostart entry, not a system service and
not a root-level service. Some environments, including Hyprland setups, do
not automatically run XDG autostart entries. In those environments, configure
the desktop entry with the session's own autostart mechanism (or explicitly run
the application with `--tray`). Creating the desktop entry alone does not
guarantee that a particular compositor will launch it.

When running from an AppImage, keep the AppImage at the same location after
enabling login startup. The entry records the absolute path from the runtime's
`APPIMAGE` environment variable and starts it with
`--appimage-extract-and-run`, so login does not depend on a temporary mount or
FUSE being available.

Disabling startup removes only Nova Mic Switch's own entry. On Linux, an
unmarked file or symlink at the owned filename is treated as a collision and
left untouched. On Windows, only the `NovaMicSwitch` value is removed.
