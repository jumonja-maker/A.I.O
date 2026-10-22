; A.I.O + Converter Guard v0.7 isolated QA. No Advanced.
; Dedicated test install; never overwrite standalone LibreOffice or Clean QA.

#define AppName "A.I.O (Guard QA)"
#define AppVersion "0.7.0-guard-qa"

[Setup]
AppId={{39D44E94-0C7E-4E26-8A5A-87021BB0B493}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\A.I.O-Guard-QA
DefaultGroupName=A.I.O Guard QA
OutputDir=..\..\build\installer
OutputBaseFilename=AIO-Guard-QA-win-x64
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\..\assets\brand\icons\aio.ico
UninstallDisplayIcon={app}\_internal\assets\brand\icons\aio.ico
DisableProgramGroupPage=yes
; Let Restart Manager request application closure before replacing installed files.
; Never forcibly terminate a running session with potentially unsaved work.
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=A.I.O + LibreOffice + Converter Guard (QA)

[Files]
Source: "..\..\dist\A.I.O\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Inno already uninstalls every logged file, including the bundled LibreOffice.
; Extra scoped cleanup handles files created under the app-owned runtime tree.
; Do NOT delete {localappdata}, profiles, user exports, another AppId, or {app} recursively.
[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\A.I.O (Guard QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0
Name: "{autodesktop}\A.I.O (Guard QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Run]
Filename: "{app}\A.I.O.exe"; Description: "Запустить A.I.O + Guard QA"; Flags: nowait postinstall skipifsilent unchecked
