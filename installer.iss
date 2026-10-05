#define MyAppName "Mafin Launcher"
#define MyAppVersion "1.7"
#define MyAppPublisher "Maksim Mafin"
#define MyAppExeName "MafinLauncher.exe"
#define JavaInstallerURL "https://api.adoptium.net/v3/installer/latest/17/ga/windows/x64/jre/hotspot/normal/eclipse"

[Setup]
AppId={{9F1B7A2E-6B7A-4E2C-9C3F-MAFINLAUNCHER}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=MafinLauncherSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
#ifdef MyIconFile
SetupIconFile={#MyIconFile}
#endif

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "dist\MafinLauncher.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\opus.dll"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; GroupDescription: "Дополнительно:"

[Code]
var
  NeedJava: Boolean;
  JavaInstallerPath: String;

function IsJavaInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  if RegKeyExists(HKLM, 'SOFTWARE\JavaSoft\JRE') or
     RegKeyExists(HKLM, 'SOFTWARE\JavaSoft\Java Runtime Environment') or
     RegKeyExists(HKLM, 'SOFTWARE\Eclipse Adoptium\JRE') or
     RegKeyExists(HKLM, 'SOFTWARE\Eclipse Adoptium\JDK') then
  begin
    Result := True;
    Exit;
  end;
  if Exec('cmd.exe', '/C java -version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0);
end;

procedure InitializeWizard();
begin
  NeedJava := not IsJavaInstalled();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpReady) and NeedJava then
  begin
    WizardForm.StatusLabel.Caption := 'Скачивание Java...';
    WizardForm.ProgressGauge.Style := npbstMarquee;
    try
      DownloadTemporaryFile('{#JavaInstallerURL}', 'java_setup.msi', '', nil);
      JavaInstallerPath := ExpandConstant('{tmp}\java_setup.msi');
    except
      RaiseException('Не удалось скачать Java. Проверьте подключение к интернету.');
    end;
    WizardForm.StatusLabel.Caption := '';
    WizardForm.ProgressGauge.Style := npbstNormal;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and NeedJava then
  begin
    WizardForm.StatusLabel.Caption := 'Установка Java...';
    Exec(JavaInstallerPath, '/quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    WizardForm.StatusLabel.Caption := '';
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    AppDir := ExpandConstant('{app}');
    if DirExists(AppDir) then
    begin
      if MsgBox('Удалить также папку "' + AppDir + '" со всеми файлами внутри' + #13#10 +
                '(настройки, скачанные версии Minecraft, моды, миры)?' + #13#10#13#10 +
                'Если нажать "Нет" - лаунчер удалится, но эта папка со всем содержимым останется на диске.',
                mbConfirmation, MB_YESNO) = IDYES then
      begin
        DelTree(AppDir, True, True, True);
      end;
    end;
  end;
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить Mafin Launcher"; \
    Flags: nowait postinstall skipifsilent
