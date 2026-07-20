; Inno Setup script for HumbleRedeemer.
;
; This wraps an already-built (and already-signed) HumbleRedeemer.exe portable
; binary into a per-user Windows installer. It does NOT build the app itself
; -- run build_release.ps1 (or the CI release workflow) first to produce and
; sign dist\HumbleRedeemer.exe, then compile this script.
;
; Usage:
;   "C:\Users\Cole\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss ^
;       /DAppVersion=1.2.0 /DSourceExe=..\dist\HumbleRedeemer.exe
;
; SourceExe defaults to ..\dist\HumbleRedeemer.exe (relative to this script)
; if not passed explicitly. Output lands in dist\HumbleRedeemer-<version>-Setup.exe.
; Sign that output separately with Azure Artifact Signing -- this script does
; not sign anything.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\dist\HumbleRedeemer.exe"
#endif

#define AppName "HumbleRedeemer"
#define AppPublisher "Slagathore"
#define AppURL "https://github.com/Slagathore/HumbleRedeemer"
#define AppIcon "..\static\icon.ico"

[Setup]
AppId={{4C6F2C3A-9B6E-4B8B-9C7A-3E6E1D2F7A45}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={localappdata}\Programs\HumbleRedeemer
DefaultGroupName=HumbleRedeemer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HumbleRedeemer-{#AppVersion}-Setup
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\HumbleRedeemer.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
#ifexist "..\LICENSE"
LicenseFile=..\LICENSE
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

; The program folder holds the exe and nothing else. User data (redeemer.db,
; the cookie files, app.log) lives in {localappdata}\HumbleRedeemer -- see
; redeemer/paths.py -- so installing, updating and uninstalling never touch
; your keys, history or sign-ins.
[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "HumbleRedeemer.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\HumbleRedeemer"; Filename: "{app}\HumbleRedeemer.exe"
Name: "{group}\Uninstall HumbleRedeemer"; Filename: "{uninstallexe}"
Name: "{autodesktop}\HumbleRedeemer"; Filename: "{app}\HumbleRedeemer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\HumbleRedeemer.exe"; Description: "Launch HumbleRedeemer"; Flags: nowait postinstall skipifsilent
; In-app updates run this installer with /SILENT, so there's no wizard page to
; tick "launch the app" on. Bring the app back up ourselves in that case.
Filename: "{app}\HumbleRedeemer.exe"; Flags: nowait postinstall; Check: WizardSilent
