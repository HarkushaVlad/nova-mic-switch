# Desktop app

The desktop app is a small tray-friendly controller for an explicitly chosen pair of microphone capture sources. The Nova 5P POWER button continues to operate headset media controls; a short press can play or pause, and press-count track actions are hardware predictions that still need validation on each headset/firmware. Holding POWER for about a second may turn the headset off rather than switch microphones.

On Linux, routing defaults to Discord-only and targets Discord's active voice-capture input. If there is no active Discord voice capture, start or join a call and try again. Windows routing changes the system default input for Discord and other applications. The app asks you to confirm the selected pair and scope before starting. The Nova 5P button trigger is only supported when the known Nova 5P HID device is present; names in the microphone list are suggestions, not device identity checks.

The app plays a short, quiet cue only after a route change is applied and verified. Cues go to the selected output and are never redirected to the system default. “Test sound” is a separate user-requested check and does not change a route. If that output disappears, cues remain disabled until you choose an available output.

“Resume listening when Nova Mic Switch opens” starts checked for a first-time setup, but is not saved as consent until you confirm and start a microphone pair. On later launches, that confirmed pair reconnects in the background if devices are temporarily unavailable. “Start Nova Mic Switch when I sign in” reads and changes this user's native startup entry only when you explicitly change the setting. “Open minimized in the tray” controls that entry; if no system tray is available, the app stays visible. On Linux this uses the user's XDG autostart folder, not a system service; some window managers may also need their own autostart setting to show tray icons.

For development checks, use `QT_QPA_PLATFORM=offscreen` and `--safe-mode --config-dir <isolated-profile>`; safe mode does not enumerate audio outputs, open HID, route audio, or write settings.
