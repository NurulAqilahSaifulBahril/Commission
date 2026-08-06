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
#define ShellExe "shell\Commission Portal.exe"

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
; Shortcuts open the Commission Portal shell (native window). Launch
; Dashboard.bat is still installed as a fallback launcher but gets no shortcut.
Name: "{group}\{#AppName}"; Filename: "{app}\{#ShellExe}"; WorkingDir: "{app}"; Comment: "Open the commission dashboard"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ShellExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Generates FLASK_SECRET_KEY and prompts for the first admin account. The
; payload ships its own Python under runtime\, so this no longer builds a .venv
; or runs pip -- it finishes in seconds. Still a console window, because it
; prompts for the admin username/password and shows the access-key reminder.
Filename: "{cmd}"; Parameters: "/c """"{app}\Setup Environment.bat"""""; WorkingDir: "{app}"; StatusMsg: "Finishing setup..."; Flags: waituntilterminated
Filename: "{app}\{#ShellExe}"; Description: "Start the dashboard now"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Created after install, so Inno does not track them.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\runtime\Lib\site-packages\__pycache__"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\8. Web Dashboard\__pycache__"
Type: files; Name: "{app}\8. Web Dashboard\dashboard.log"

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  // The payload carries its own Python (runtime\), so an interpreter on the
  // machine is no longer required -- this used to be a hard gate that sent
  // non-technical staff off to python.org before they could install anything.
  // The check is kept only as a courtesy for source checkouts, where
  // "Setup Environment.bat" still has to build a .venv, and it never blocks.
  if not FileExists(ExpandConstant('{src}\runtime\python.exe')) then
  begin
    if not Exec('cmd.exe', '/c where py >nul 2>&1 || where python >nul 2>&1', '',
                SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      if MsgBox('This installer does not appear to include the bundled Python '
                + 'runtime, and no Python was found on this computer.' + #13#10#13#10 +
                'Install Python 3.10 or newer from python.org (tick '
                + '"Add python.exe to PATH"), or ask IT for a full installer.' + #13#10#13#10 +
                'Continue anyway?',
                mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;
