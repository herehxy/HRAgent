#Requires -Version 5.1
# HRAgent 工作台 - 安装脚本
#
# 为什么用 PowerShell 而不是让安装器自己解压：
#   现场机器上，PyInstaller 打包的**未签名 exe 无法写磁盘**（安全策略），
#   而 robocopy / PowerShell 是微软签名工具，不受这条限制。
#   所以把"复制文件 + 建快捷方式 + 写卸载信息"全部交给系统工具做。
#
# 用法：
#   双击同目录的「安装.cmd」        —— 交互式，一路回车即可
#   静默部署（给多台机器批量装）：
#     powershell -ExecutionPolicy Bypass -File install.ps1 `
#         -Target "D:\HRAgent工作台" -Legacy "D:\旧版目录" -NoLaunch

param(
    [string]$Target,
    [string]$Legacy,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$Src = $PSScriptRoot
$ExeName = 'HRAgent工作台.exe'

# 版本号从 version.json 读，避免脚本里的写死值与实际包不一致
$Ver = '3.1.15'
$verFile = Join-Path $Src 'version.json'
if (Test-Path $verFile) {
    try { $Ver = (Get-Content $verFile -Raw -Encoding UTF8 | ConvertFrom-Json).version } catch { }
}

function Write-Step($t) { Write-Host "  $t" }
function Fail($msg) {
    Write-Host ''
    Write-Host "  [x] $msg" -ForegroundColor Red
    Write-Host ''
    Read-Host '按回车退出'
    exit 1
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '   HRAgent 工作台  安装' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path (Join-Path $Src $ExeName))) {
    Fail "当前目录里找不到 $ExeName。请把本脚本放在解压后的 HRAgent工作台 文件夹里运行。"
}

# ---------------- 1. 安装位置 ----------------
$Default = Join-Path $env:LOCALAPPDATA 'Programs\HRAgent工作台'
if ([string]::IsNullOrWhiteSpace($Target)) {
    Write-Host "  安装位置（直接回车 = $Default）"
    $Target = Read-Host '  安装到'
    if ([string]::IsNullOrWhiteSpace($Target)) { $Target = $Default }
}
$Target = $Target.Trim().Trim('"').TrimEnd('\')

$sameFolder = ($Target -eq $Src)
Write-Host ''
Write-Host "  安装到： $Target" -ForegroundColor Green
Write-Host ''

# ---------------- 2. 复制程序 ----------------
if ($sameFolder) {
    Write-Step '安装位置就是当前文件夹，跳过复制。'
} else {
    Write-Step '[1/3] 复制程序文件…'
    if (-not (Test-Path $Target)) { New-Item -ItemType Directory -Path $Target -Force | Out-Null }
    $r = robocopy $Src $Target /E /R:1 /W:1 /NFL /NDL /NJH /NJS /XF *.log
    if ($LASTEXITCODE -ge 8) { Fail "复制失败（robocopy 返回 $LASTEXITCODE）。请换一个位置，例如 D:\HRAgent工作台" }
    Write-Step '      完成'
}

# ---------------- 3. 迁移旧数据 ----------------
Write-Host ''
Write-Host '  [2/3] 迁移旧版数据（可选）'
if ([string]::IsNullOrWhiteSpace($Legacy)) {
    Write-Host '        如果有旧版 HRAgent，把它的安装目录拖进来。'
    Write-Host '        全新安装直接回车跳过。'
    $Old = Read-Host '  旧版目录'
} else {
    $Old = $Legacy
}
$Old = $Old.Trim().Trim('"').TrimEnd('\')

if (-not [string]::IsNullOrWhiteSpace($Old)) {
    if (-not (Test-Path $Old)) {
        Write-Step "      [x] 目录不存在：$Old（跳过数据迁移）"
    }
}

if (-not [string]::IsNullOrWhiteSpace($Old) -and (Test-Path $Old)) {
    $OldData = $null; $OldConf = $null
    if (Test-Path (Join-Path $Old '_internal\data\workbench.db')) {
        $OldData = Join-Path $Old '_internal\data'; $OldConf = Join-Path $Old '_internal\config'
    } elseif (Test-Path (Join-Path $Old 'data\workbench.db')) {
        $OldData = Join-Path $Old 'data'; $OldConf = Join-Path $Old 'config'
    }
    if (-not $OldData) {
        Write-Step '      找不到 workbench.db，跳过数据迁移。'
    } elseif (Test-Path (Join-Path $Target 'data\workbench.db')) {
        Write-Step '      目标目录已有数据库，跳过迁移以免覆盖。'
    } else {
        Write-Step "      旧数据： $OldData"
        robocopy $OldData (Join-Path $Target 'data') /E /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null
        if ($OldConf -and (Test-Path $OldConf)) {
            robocopy $OldConf (Join-Path $Target 'config') /E /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null
        }
        Write-Step '      [√] 数据与配置已迁移'
    }
}

# ---------------- 4. 快捷方式 + 卸载信息 ----------------
Write-Host ''
Write-Step '[3/3] 创建快捷方式…'
$shell = New-Object -ComObject WScript.Shell
$exePath = Join-Path $Target $ExeName
$made = 0

foreach ($dir in @([Environment]::GetFolderPath('Desktop'),
                   (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    if (-not (Test-Path $dir)) { continue }
    try {
        $lnk = Join-Path $dir 'HRAgent工作台.lnk'
        $s = $shell.CreateShortcut($lnk)
        $s.TargetPath = $exePath
        $s.WorkingDirectory = $Target
        $s.IconLocation = "$exePath,0"
        $s.Description = 'HRAgent 工作台 - 西北有色院人事'
        $s.Save()
        $made++
    } catch { Write-Step "      （$dir 创建失败：$($_.Exception.Message)）" }
}
Write-Step "      已创建 $made 个快捷方式"

# 卸载入口：控制面板能看到，也留一个「卸载.cmd」
$unins = Join-Path $Target '卸载.cmd'
try {
    Set-Content -Path $unins -Encoding ASCII -Value @"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
"@
} catch { }
try {
    $key = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench'
    New-Item -Path $key -Force | Out-Null
    Set-ItemProperty -Path $key -Name 'DisplayName' -Value 'HRAgent 工作台'
    Set-ItemProperty -Path $key -Name 'DisplayVersion' -Value $Ver
    Set-ItemProperty -Path $key -Name 'InstallLocation' -Value $Target
    Set-ItemProperty -Path $key -Name 'DisplayIcon' -Value "$exePath,0"
    Set-ItemProperty -Path $key -Name 'UninstallString' -Value ('"' + $unins + '"')
    Set-ItemProperty -Path $key -Name 'Publisher' -Value '西北有色金属研究院'
    Set-ItemProperty -Path $key -Name 'NoModify' -Value 1 -Type DWord
    Set-ItemProperty -Path $key -Name 'NoRepair' -Value 1 -Type DWord
    Write-Step '      已写入卸载信息（控制面板可卸载）'
} catch { Write-Step "      （卸载信息写入失败：$($_.Exception.Message)）" }

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '   [√] 安装完成' -ForegroundColor Green
Write-Host ''
Write-Host "   程序位置： $Target"
Write-Host '   首次登录账号： hr'
Write-Host '   首次登录取码： change-me'
Write-Host '   （登录后请立即在「账号管理」里改掉）'
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
$ans = if ($NoLaunch) { 'N' } else { Read-Host '  现在启动吗？(Y/N)' }
if ($ans -match '^[Yy]') {
    Start-Process -FilePath $exePath -WorkingDirectory $Target
}
