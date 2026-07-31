; Inno Setup script for the Finance Commission Dashboard first-time installer.
;
; Every update after this one is delivered over the air by the app itself
; (see "8. Web Dashboard/updater.py"), so this installer only has to get the
; first copy onto the machine and build its Python environment.
;
; Build:
;   python tools\build_package.py --version 1.2.0
;   ISCC.exe /DAppVersion=1.2.0 installer\CommissionDashboard.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "Finance Commission Dashboard"
#define Publisher "Eternalgy"
#define LaunchBat "Launch Dashboard.bat"

[Setup]
AppId={{9C2F4B71-6D3E-4A55-9F80-3A17C6E2D5B4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL=https://github.com/NurulAqilahSaifulBahril/Commission
DefaultDirName={autopf}\Eternalgy\Commission Dashboard
DefaultGroupName=Eternalgy
DisableProgramGroupPage=yes
; Per-user install: no admin prompt, and the app can update its own files later.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CommissionDashboard-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; dist\payload is produced by tools\build_package.py — code only, no workbooks,
; no database, no .env.
Source: "..\dist\payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#LaunchBat}"; WorkingDir: "{app}"; Comment: "Open the commission dashboard"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#LaunchBat}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Builds .venv, installs dependencies, generates FLASK_SECRET_KEY and prompts
; for the first admin account. Shown in a console window so the user can see
; pip working and read the PG_PROXY_TOKEN reminder at the end.
Filename: "{cmd}"; Parameters: "/c """"{app}\Setup Environment.bat"""""; WorkingDir: "{app}"; StatusMsg: "Setting up the Python environment (this can take a few minutes)..."; Flags: waituntilterminated
Filename: "{app}\{#LaunchBat}"; Description: "Start the dashboard now"; WorkingDir: "{app}"; Flags: postinstall nowait shellexec skipifsilent

[UninstallDelete]
; Created after install, so Inno does not track them.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\8. Web Dashboard\__pycache__"
Type: files; Name: "{app}\8. Web Dashboard\dashboard.log"

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  // Python is a hard requirement — the dashboard runs from source in a venv.
  if not Exec('cmd.exe', '/c where py >nul 2>&1 || where python >nul 2>&1', '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    if MsgBox('Python was not found on this computer.' + #13#10#13#10 +
              'The dashboard needs Python 3.10 or newer. Install it from ' +
              'python.org (tick "Add python.exe to PATH"), then run this setup again.' +
              #13#10#13#10 + 'Continue anyway?',
              mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
