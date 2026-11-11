#define AppName "Nova Mic Switch"
#ifndef AppVersion
#define AppVersion "0.1.0"
#endif
#define AppExe "NovaMicSwitch.exe"

[Setup]
AppId={{C8459C76-6E79-4F2C-A1DC-6377135F03A2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Nova Mic Switch
DefaultDirName={localappdata}\Programs\Nova Mic Switch
DefaultGroupName=Nova Mic Switch
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\..\dist\installers
OutputBaseFilename=NovaMicSwitch-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#AppExe}
; This installer is per-user and does not require elevation.

[Tasks]
Name: "startup"; Description: "Start Nova Mic Switch when I sign in (in the system tray)"; GroupDescription: "Startup:"

[Files]
Source: "..\..\dist\windows\NovaMicSwitch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Nova Mic Switch"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall Nova Mic Switch"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NovaMicSwitch"; ValueData: """{app}\{#AppExe}"" --tray"; Tasks: startup; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "NovaMicSwitch"; Tasks: not startup; Flags: deletevalue

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--tray"; Description: "Launch Nova Mic Switch"; Flags: postinstall nowait skipifsilent
