; Inno Setup script for Twitch Shorts Studio. build_installer.bat passes AppVersion.
; Installs for the current user (no administrator needed); settings, models and
; history stay in %LOCALAPPDATA%\TwitchShortsStudio and survive an update or uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Twitch Shorts Studio"
#define AppExe "TwitchShortsStudio.exe"

[Setup]
AppId={{6F1C2B7E-5D3A-4C9E-9B21-7A4E8F2D6C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=skof
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\installer
OutputBaseFilename=TwitchShortsStudio-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\build\dist\TwitchShortsStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
