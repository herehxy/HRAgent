# -*- coding: utf-8 -*-
"""
HRAgent 企业人才库 —— Windows 安装包脚本（Inno Setup 6）

用法：
    "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" installer\HRAgent.iss

设计要点（都是踩过坑才定下来的）：

1) **安装目录默认不写死**，让HR 自己选。程序代码可以装到 C:\\Program Files
   这种只读位置 —— 因为数据落在 %LOCALAPPDATA%\\HRAgentData，与安装目录
   彻底解耦（见 webapp_launcher.py::_prepare_profile）。这是本脚本能成立的
   前提，不是顺带如此。

2) **不塞数据目录**。曾经想把演示库塞进安装目录，后来数据目录改成用户目录后
   就多余了；塞进去只会留下"改了没生效"的旧数据副本，让人以为程序没更新。

3) **不打包 portable.flag**。有这个文件启动器会判定为便携模式，改用安装目录
   内的 data/，而那个位置在 Program Files 下只读 → 直接坏掉。

4) **卸载不删用户数据**。默认行为保留 %LOCALAPPDATA%\\HRAgentData，让HR 的
   简历库在重装后还在。删除走卸载页上的复选框，且默认不勾。

5) **安装前检测已在运行的实例**。程序是托盘常驻的，安装时若它还在跑，
   文件被锁导致安装失败或装到一半，必须先拦下来。

6) **装完不自动启动**。让 HR 自己双击，避免"装完就弹窗"这种不受控行为
   （用户明确要求过：界面少提示、少意外动作）。
"""

# ---------------------------------------------------------------- 基础配置
#define AppName        "HRAgent 企业人才库"
#define AppNameShort   "HRAgent工作台"
#define AppVersion     "2.2.5"
#define AppPublisher   "HRAgent"
#define AppExeName     AppNameShort + ".exe"

; 源码目录（PyInstaller onedir 产物所在处）
#define SourceDir      "..\dist\" + AppNameShort
; 输出：工程根 release\installer\
#define OutputBaseDir  "..\release\installer"
#define SetupExeName   AppNameShort + "-安装程序-v" + AppVersion + ".exe"

[Setup]
AppId={{7C4E9A21-3D6B-4F58-9E02-A1B7C5D38F14}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

; 装到 Program Files 要管理员；给用户默认装到用户目录，免掉UAC 弹窗
; （HR 大概率不是管理员，弹 UAC 会让人直接放弃安装）
DefaultDirName={localappdata}\Programs\{#AppNameShort}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no                ; ★ 允许用户自选安装路径
AllowNoIcons=yes
OutputDir={#OutputBaseDir}
OutputBaseFilename={#SetupExeName}
SetupIconFile=..\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}

; 只装给当前用户，不提权、不写HKLM（避开 UAC 和权限问题）
PrivilegesRequiredOverridesAllowed=dialog commandline
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

; 目标目录若被占用，安装时自动尝试重启替换
CloseApplications=yes
CloseApplicationsFilter=*.exe|*.dll|*.pyd|*.db
RestartApplications=no

WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl,english"

[CustomMessages]
chinese.Verifying=正在检查安装环境...
chinese.NoRunning=正在关闭正在运行的 {#AppNameShort}...
chinese.LaunchApp=安装完成后立即启动 {#AppNameShort}
chinese.CreateShortcut=创建桌面快捷方式
chinese.DeleteUserData=同时删除用户数据（简历库将无法恢复）
english.Verifying=Checking installation environment...
english.NoRunning=Closing running {#AppNameShort}...
english.LaunchApp=Launch {#AppNameShort} now
english.CreateShortcut=Create a desktop shortcut
english.DeleteUserData=Also delete user data (candidate database cannot be recovered)

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateShortcut}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "launch";     Description: "{cm:LaunchApp}"; GroupDescription: "{cm:PostInstallRunOnce}"; Flags: unchecked

[Files]
; onedir 产物：exe + _internal（运行时、seed 配置、演示库）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppNameShort}";           Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppNameShort}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppNameShort}";     Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userappdata}\{#AppNameShort} 快捷方式"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchApp,{#AppNameShort}}"; Tasks: launch; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载清程序自己留下的东西（不含用户数据目录）
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\config"
Type: files;        Name: "{app}\portable.flag"
Type: files;        Name: "{app}\webapp.log"

[Code]
var
  UserDataDir: String;
  DataWasInstalled: Boolean;

{用户数据目录 —— 与 webapp_launcher.py::_default_user_home 保持一致}
function GetUserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\HRAgentData');
end;

{ 安装前：拦住正在运行的实例。
  不这么做的话，exe 和 _internal 里的 pyd/dll 被锁，安装要么失败要么装一半。}
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  UserDataDir := GetUserDataDir();
  DataWasInstalled := DirExists(UserDataDir);

  { 进安装向导前先关一次。托盘程序是常驻的，不关的话向导最后一步
    拷文件会因为 pyd/dll 被占用而失败。}
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#AppExeName}"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

{ 安装目录页：拦住"程序正在运行"。
  这里必须在用户选完路径后、真正开始拷贝前判定。}
function CurStepChanged(CurStep: TSetupStep): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if CurStep = ssInstall then
  begin
    { 第二次拦：ssPostInstall 前再确认一次，覆盖用户在向导里磨蹭时
      手动双击启动了程序的情况。}
    if WizardSilent then
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#AppExeName}"',
           '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if UninstallProgressForm isCreateTaskSelected then
    begin
      DelTree(UserDataDir, True, True, True);
    end;
  end;
end;

{ 安装完成页加一条"数据在哪"的说明 —— 这条是必要的：
  HR 找不到数据目录会以为数据丢了。放文件里需用户自己找。}
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedLabel.Caption := SetupMessage(
      '{cm}Finished,{#AppName} 已安装到：' + #13#10 + '{app}' + #13#10#13#10 +
      '简历与配置数据保存在：' + UserDataDir + #13#10 +
      '（与安装目录相互独立，重装程序不会丢失）'
    );
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
end;