# Windows audio routing

The Windows adapter lists active Core Audio capture endpoints and changes the
system-wide default capture endpoint. It sets the Console, Multimedia, and
Communications roles (roles 0, 1, and 2); this is not an application-specific
Discord setting. Applications that follow Windows defaults may change input
when routing is used. Discord should be configured to use **Default** as its
input device if it is expected to follow these changes.

Setting defaults uses `IPolicyConfig::SetDefaultEndpoint`, an undocumented
Windows implementation API rather than a stable supported public setter.
Windows updates may change its availability or behavior. The adapter validates
that the requested ID is an active capture endpoint and reads all three
defaults back after setting them. If a later setter or readback fails, it
reports which roles had already been changed; it does not attempt a blind
rollback. No change is made merely by constructing the adapter or listing
devices.

## Manual Windows checks

Run these only on a Windows machine with at least two available capture
endpoints:

1. Confirm the listed IDs/names match active devices in Windows Sound settings.
2. Start a live voice call with Discord input set to **Default**. Change the
   selected endpoint and confirm the call uses that microphone without
   restarting Discord.
3. Confirm switching endpoints does not interrupt background HID input.
4. Unplug the selected microphone, inspect the reported failure/default state,
   reconnect it, and check endpoint enumeration again.
5. Restart the application and confirm it has not changed the Windows default
   until the user explicitly selects an endpoint.

Windows runtime behavior and hardware-dependent checks remain untested in a
non-Windows environment. Mocked tests validate adapter decisions, not COM ABI
or the undocumented setter on Windows.
