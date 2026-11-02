# HRAgent 工作台 - 卸载脚本
#
# 只删程序文件、快捷方式和注册表项；**用户数据（data / config）默认保留**，
# 万一以后还想用还能找回来。要连数据一起删，运行时加 -PurgeData。

param([switch]$PurgeData)

$ErrorActionPreference = 'Continue'
$Target = $PSScriptRoot

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '   HRAgent 工作台  卸载' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host "  程序目录： $Target"
Write-Host ''

# 1. 先停掉正在运行的实例
$procs = Get-Process -Name 'HRAgent工作台' -ErrorAction SilentlyContinue |
         Where-Object { $_.Path -and $_.Path.StartsWith($Target, 'OrdinalIgnoreCase') }
if ($procs) {
    Write-Host '  正在关闭运行中的工作台…'
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# 2. 删快捷方式
foreach ($dir in @([Environment]::GetFolderPath('Desktop'),
                   (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    $lnk = Join-Path $dir 'HRAgent工作台.lnk'
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force -ErrorAction SilentlyContinue
        Write-Host "  已删除快捷方式： $lnk"
    }
}

# 3. 删注册表项
$key = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench'
if (Test-Path $key) {
    Remove-Item $key -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '  已删除卸载注册项'
}

# 4. 删程序文件（默认保留 data / config）
Write-Host ''
if ($PurgeData) {
    Write-Host '  [!] 将删除全部内容，包括简历库与附件。' -ForegroundColor Yellow
} else {
    Write-Host '  将保留下面的用户数据：' -ForegroundColor Yellow
    foreach ($d in @('data', 'config')) {
        $p = Join-Path $Target $d
        if (Test-Path $p) { Write-Host "    $p" }
    }
}
$ans = Read-Host '  确认卸载吗？(Y/N)'
if ($ans -notmatch '^[Yy]') { Write-Host '  已取消。'; Read-Host '按回车退出'; exit 0 }

$keep = @()
if (-not $PurgeData) { $keep = @((Join-Path $Target 'data'), (Join-Path $Target 'config')) }

Get-ChildItem $Target -Force -ErrorAction SilentlyContinue | ForEach-Object {
    if ($keep -contains $_.FullName) { return }
    Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ''
if ($PurgeData) {
    Remove-Item $Target -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '  [√] 已完全卸载（含数据）' -ForegroundColor Green
} else {
    Write-Host '  [√] 程序已卸载，数据保留在：' -ForegroundColor Green
    foreach ($k in $keep) { if (Test-Path $k) { Write-Host "      $k" } }
    Write-Host '  确认不再需要后，可以手动删除整个目录。'
}
Write-Host ''
Read-Host '按回车退出'
