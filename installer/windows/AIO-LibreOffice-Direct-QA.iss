; A.I.O direct LibreOffice integration QA. No Guard. No Advanced.
; Deliberately separate from Clean Excel QA installation for A/B tests.

#define AppName "A.I.O (LibreOffice QA)"
#define AppVersion "0.2.0-direct-qa"

[Setup]
AppId={{846FF640-83F0-4A44-B227-6FCB43225171}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\A.I.O-LibreOffice-QA
DefaultGroupName=A.I.O LibreOffice QA
OutputDir=..\..\build\installer
OutputBaseFilename=AIO-LibreOffice-Direct-QA-win-x64
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\..\assets\brand\icons\aio.ico
UninstallDisplayIcon={app}\_internal\assets\brand\icons\aio.ico
DisableProgramGroupPage=yes
UninstallDisplayName=A.I.O LibreOffice Direct QA (no Guard)

[Files]
Source: "..\..\dist\A.I.O\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\A.I.O (LibreOffice QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0
Name: "{autodesktop}\A.I.O (LibreOffice QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Run]
Filename: "{app}\A.I.O.exe"; Description: "Запустить A.I.O LibreOffice QA"; Flags: nowait postinstall skipifsilent unchecked
