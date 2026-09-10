#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef SourceExe
  #error SourceExe is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef OutputBaseName
  #error OutputBaseName is required
#endif

#define AppName "Dayfinch Tracker"
#define AppPublisher "Dayfinch"
#define AppExecutable "Dayfinch-Agent.exe"

[Setup]
AppId={{676530E5-D938-4A09-AB45-878E680742F4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Dayfinch
DefaultGroupName=Dayfinch
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
UninstallDisplayIcon={app}\{#AppExecutable}
UninstallDisplayName={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Visible employee time and activity tracker
VersionInfoProductName={#AppName}

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "{#AppExecutable}"; Flags: ignoreversion

[Icons]
Name: "{group}\Dayfinch Tracker"; Filename: "{app}\{#AppExecutable}"
Name: "{autodesktop}\Dayfinch Tracker"; Filename: "{app}\{#AppExecutable}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#AppExecutable}"; Description: "Open Dayfinch Tracker"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Employee enrollment and offline data intentionally survive uninstall. Removing
; them requires an explicit user/admin decision rather than an installer side effect.
Type: filesandordirs; Name: "{app}"
