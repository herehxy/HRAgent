# HRAgent migration: C:\Users\admin\WorkBuddy\HRAgent -> E:\WorkBuddy\HRAgent_v2
# Copy source/config/memory/build-scripts only. Skip historical build artifacts.
# NOTE: keep this file pure ASCII. PowerShell 5.1 reads .ps1 as GBK when there
# is no BOM, so any CJK comment here corrupts into a parser error.
$ErrorActionPreference = 'Stop'

$src = 'C:\Users\admin\WorkBuddy\HRAgent'
$dst = 'E:\WorkBuddy\HRAgent_v2'

$skipTop = @(
  'release','dist','build','build_bad_1500','git_work','__pycache__',
  '_tmp','_tmp_edge_cdp','_pack_data','_preview_data','_preview',
  '_baseline_run','_baseline_src','_conc_src','_conc_test',
  'repo_latest','node_modules','_memory_backup_before_clean'
)
$skipTop += (Get-ChildItem $src -Directory -Filter 'build_old_*'| ForEach-Object { $_.Name })
$skipTop += (Get-ChildItem $src -Directory -Filter '_shots*' | ForEach-Object { $_.Name })
$skipTop += (Get-ChildItem $src -Directory -Filter 'backup_*'     | ForEach-Object { $_.Name })
$skipTop += (Get-ChildItem $src -Directory -Filter '_backup_files_before_*' | ForEach-Object { $_.Name })

# inside repo_src: verify-temp output + two duplicate runtimes
$skipInRepo = @('_prod_verify','.runtime_deps','.runtime_local','__pycache__','node_modules')

$xd = @()
foreach ($d in $skipTop)    { $xd += (Join-Path $src $d) }
foreach ($d in $skipInRepo) { $xd += (Join-Path $src "repo_src\$d") }

$rcArgs = @($src, $dst, '/E', '/XD') + $xd + @(
  '/XF','*.pyc','*.pyo',
  '/R:1','/W:1',
  '/NFL','/NDL','/NJH','/NP',
  '/MT:8'
)

Write-Host ("excluding {0} dir(s)" -f $xd.Count)
Write-Host '--- copying ---'
& robocopy @rcArgs | Out-Null
$code = $LASTEXITCODE
# robocopy: 0=none 1=copied 2=deleted 3=both >=8=failed
if ($code -ge 8) { throw "robocopy failed, exit=$code" }
Write-Host ("robocopy exit={0} (1=files copied, normal)" -f $code)

$fs = Get-ChildItem $dst -Recurse -File -Force
$sz = ($fs | Measure-Object -Property Length -Sum).Sum
Write-Host ("DONE on E: {0} files, {1:N1} MB" -f $fs.Count, ($sz/1MB))