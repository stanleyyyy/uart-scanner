; ===========================================================================
; Inno Setup script for the UART Scanner
; Produces a single, shareable Setup exe:  installer\Output\UART-Scanner-Setup.exe
;
; Build:  iscc installer\uart-scanner.iss     (or run installer\build_installer.bat)
;
; Bundles the standalone uart_scan.exe, so target machines need NOTHING else
; (no Python). Installs per-user (no admin prompt) and creates Start Menu +
; optional Desktop shortcuts that launch through the flash-free VBS.
; ===========================================================================

#define AppName    "UART Scanner"
#define AppVersion "1.0.0"
#define AppExe     "uart_scan.exe"

[Setup]
AppId={{7C2B0E2A-9C3D-4E7A-8F1B-A1B2C3D4E5F6}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Stanislav Ruzani
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install -> no UAC prompt; change to "admin" to install for all users.
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=UART-Scanner-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\dist\{#AppExe}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The standalone app (built by build.bat -> dist\uart_scan.exe).
Source: "..\dist\{#AppExe}";        DestDir: "{app}\dist"; Flags: ignoreversion
; Launchers.
Source: "..\scripts\launch.vbs";    DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\run_app.bat";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\uart_scan.bat";         DestDir: "{app}"; Flags: ignoreversion
; Docs.
Source: "..\README.md";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE";               DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Launch through wscript+launch.vbs (flash-free, pre-titled window).
Name: "{group}\{#AppName}"; Filename: "{sys}\wscript.exe"; \
    Parameters: """{app}\scripts\launch.vbs"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\dist\{#AppExe}"; IconIndex: 0
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{sys}\wscript.exe"; \
    Parameters: """{app}\scripts\launch.vbs"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\dist\{#AppExe}"; IconIndex: 0; Tasks: desktopicon

[Run]
; Offer to launch right after install.
Filename: "{sys}\wscript.exe"; Parameters: """{app}\scripts\launch.vbs"""; \
    Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent
