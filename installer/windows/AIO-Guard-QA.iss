; A.I.O + Converter Guard v0.4 integration QA. No Advanced.
; Dedicated test install; never overwrite standalone LibreOffice or Clean QA.

#define AppName "A.I.O (Guard QA)"
#define AppVersion "0.4.0-guard-qa"

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
UninstallDisplayName=A.I.O + LibreOffice + Converter Guard (QA)

[Files]
Source: "..\..\dist\A.I.O\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\A.I.O (Guard QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0
Name: "{autodesktop}\A.I.O (Guard QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Run]
Filename: "{app}\A.I.O.exe"; Description: "Запустить A.I.O + Guard QA"; Flags: nowait postinstall skipifsilent unchecked
