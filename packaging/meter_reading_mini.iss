; 必須定義: /DAppVersion=<版数> /DOutputName=<名前> /DBundleDir=<検証済み配置>

#ifndef AppVersion
  #error "AppVersion を指定してください。"
#endif
#ifndef OutputName
  #error "OutputName を指定してください。"
#endif
#ifndef BundleDir
  #error "BundleDir を指定してください。"
#endif

#define AppName "検針管理 mini"
#define AppExeName "meter-reading-mini.exe"

[Setup]
AppId={{E78AA3AE-60B5-47C4-9EFA-32AE6BDF0C44}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\meter-reading-mini
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#SourcePath}\licenses\GPL-3.0.txt
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=no
RestartApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{userprograms}\{#AppName} のライセンス"; Filename: "{app}\licenses\GPL-3.0.txt"
Name: "{userprograms}\{#AppName} の第三者通知"; Filename: "{app}\THIRD_PARTY_NOTICES.txt"
Name: "{userprograms}\{#AppName} の対応ソース"; Filename: "{app}\SOURCE_CODE.txt"

[Code]
const
  MRM_GENERIC_READ_WRITE = $C0000000;
  MRM_OPEN_ALWAYS = 4;
  MRM_FILE_ATTRIBUTE_NORMAL = $80;
  MRM_ERROR_SHARING_VIOLATION = 32;
  MRM_ERROR_LOCK_VIOLATION = 33;

var
  GuardHandle: Integer;

function CreateFileW(Name: String; Access, Share: Cardinal; Security: Integer;
  Creation, Flags: Cardinal; Template: Integer): Integer;
  external 'CreateFileW@kernel32.dll stdcall';

function CloseHandle(Handle: Integer): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function AcquireGuard(ActionName: String): Boolean;
var
  DirectoryName, GuardPath, MessageText: String;
  ErrorCode: Integer;
begin
  Result := False;
  GuardHandle := -1;
  DirectoryName := ExpandConstant('{localappdata}\meter-reading-mini');
  if not ForceDirectories(DirectoryName) then begin
    MsgBox('更新・削除ガードの保存先を作成できません。アクセス権を確認してください。', mbError, MB_OK);
    exit;
  end;
  GuardPath := AddBackslash(DirectoryName) + 'installation.guard';
  GuardHandle := CreateFileW(GuardPath, MRM_GENERIC_READ_WRITE, 0, 0, MRM_OPEN_ALWAYS,
    MRM_FILE_ATTRIBUTE_NORMAL, 0);
  if GuardHandle = -1 then begin
    ErrorCode := DLLGetLastError;
    if (ErrorCode = MRM_ERROR_SHARING_VIOLATION) or
      (ErrorCode = MRM_ERROR_LOCK_VIOLATION) then
      MessageText := 'アプリケーションまたは別のインストーラーが実行中です。終了してから' +
        ActionName + 'をやり直してください。'
    else
      MessageText := '更新・削除ガードを確保できません。' + #13#10 + SysErrorMessage(ErrorCode);
    MsgBox(MessageText, mbError, MB_OK);
    exit;
  end;
  Result := True;
end;

procedure ReleaseGuard;
begin
  if (GuardHandle <> -1) and (GuardHandle <> 0) then begin
    if not CloseHandle(GuardHandle) then
      MsgBox('更新・削除ガードを解放できません。' + #13#10 +
        SysErrorMessage(DLLGetLastError), mbError, MB_OK);
    GuardHandle := -1;
  end;
end;

function InitializeSetup: Boolean;
begin
  Result := AcquireGuard('インストールまたは更新');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CompareText(RemoveBackslashUnlessRoot(ExpandConstant('{app}')),
    RemoveBackslashUnlessRoot(ExpandConstant('{localappdata}\Programs\meter-reading-mini'))) <> 0 then
    Result := 'インストール先はユーザー専用の既定フォルダーである必要があります。';
end;

procedure DeinitializeSetup;
begin
  ReleaseGuard;
end;

function InitializeUninstall: Boolean;
begin
  Result := AcquireGuard('アンインストール');
end;

procedure DeinitializeUninstall;
begin
  ReleaseGuard;
end;
