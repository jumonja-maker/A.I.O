; Clean A.I.O UI+Excel QA installer — does NOT contain LibreOffice engine.
; Never publish as finished converter release until Converter Guard is ready.

#define AppName "A.I.O"
#define AppVersion "0.1.0-preview"

[Setup]
AppId={{11B96028-564E-4F8D-B94F-E3571E900198}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\A.I.O
DefaultGroupName=A.I.O
OutputDir=..\..\build\installer
OutputBaseFilename=AIO-Clean-UI-Excel-QA-win-x64
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=A.I.O (Clean QA)
WizardStyle=modern
SetupIconFile=..\..\assets\brand\icons\aio.ico
UninstallDisplayIcon={app}\_internal\assets\brand\icons\aio.ico
DisableProgramGroupPage=yes

[Files]
Source: "..\..\dist\A.I.O\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\A.I.O (Clean QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0
Name: "{autodesktop}\A.I.O (Clean QA)"; Filename: "{app}\A.I.O.exe"; IconFilename: "{app}\_internal\assets\brand\icons\aio.ico"; IconIndex: 0; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Run]
Filename: "{app}\A.I.O.exe"; Description: "Запустить A.I.O"; Flags: nowait postinstall skipifsilent unchecked
