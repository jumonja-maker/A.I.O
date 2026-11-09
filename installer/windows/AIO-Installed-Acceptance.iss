; Disposable, per-user installed-GUI acceptance. Not a release installer.
; All variable values come from the validated source-bound builder.
#ifndef AIOPayload
  #error AIOPayload must be supplied by build_completion_installer.py
#endif
#ifndef AIOSourceReceipt
  #error AIOSourceReceipt must be supplied by build_completion_installer.py
#endif
#ifndef AIOOutput
  #error AIOOutput must be supplied by build_completion_installer.py
#endif
#ifndef AIOSourceSHA
  #error AIOSourceSHA must be supplied by build_completion_installer.py
#endif
#ifndef AIOVersion
  #error AIOVersion must be supplied by build_completion_installer.py
#endif

[Setup]
AppId={{C1B5474A-4A1D-42EC-B2CD-A57A2011F34E}
AppName=A.I.O Installed Acceptance
AppVersion={#AIOVersion}
VersionInfoVersion=2026.11.9.0
DefaultDirName={localappdata}\Programs\AIO-Installed-Acceptance-{#Copy(AIOSourceSHA, 1, 12)}
DefaultGroupName=A.I.O Installed Acceptance
OutputDir={#AIOOutput}
OutputBaseFilename=AIO-Installed-Acceptance-{#Copy(AIOSourceSHA, 1, 12)}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
DisableProgramGroupPage=yes
UsePreviousAppDir=no
UsePreviousGroup=no
UsePreviousTasks=no
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=A.I.O Installed Acceptance
UninstallDisplayIcon={app}\A.I.O.exe
SetupLogging=yes

[Files]
Source: "{#AIOPayload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AIOSourceReceipt}"; DestDir: "{app}"; DestName: "AIO-install-source.json"; Flags: ignoreversion

[Tasks]
Name: desktopicon; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Icons]
Name: "{autodesktop}\A.I.O Installed Acceptance"; Filename: "{app}\A.I.O.exe"; Tasks: desktopicon

; Only Inno's logged payload and receipt are removed. There is deliberately
; no UninstallDelete, external data cleanup, recursive {app} cleanup, or Run.
; Unlogged app-root files and all external exports survive uninstallation.
