# ---------------------------------------------------------------------------
# Update this project from GitHub, and verify that it worked.
#
# Written because two updates silently did nothing. `Copy-Item -Recurse` into a
# directory that already exists can nest the source inside the destination
# instead of merging -- `src` becomes `wcl\src\src` -- so new files land where
# Python never looks while the old code keeps running and the only symptom is a
# command that "does not exist".
#
# robocopy merges correctly, and this script refuses to report success without
# checking the installed version afterwards.
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

$Branch  = "claude/sweet-johnson-m8xjee"
$Repo    = "teeeeq-collab/Warcraft-logs-api-builder"
$Project = $PSScriptRoot
if (-not $Project) { $Project = (Get-Location).Path }

$Zip     = Join-Path $env:TEMP "wcl-update.zip"
$Unzip   = Join-Path $env:TEMP "wcl-update"

Write-Host "Project folder : $Project"
Write-Host "Branch         : $Branch`n"

# -- before -----------------------------------------------------------------
$before = "unknown"
$exe = Join-Path $Project ".venv\Scripts\wclmplus.exe"
if (Test-Path $exe) {
    try { $before = (& $exe version | ConvertFrom-Json).software_version } catch { }
}
Write-Host "Installed version before: $before"

# -- download ---------------------------------------------------------------
Write-Host "`nDownloading..."
if (Test-Path $Zip)   { Remove-Item $Zip -Force }
if (Test-Path $Unzip) { Remove-Item $Unzip -Recurse -Force }
Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $Zip
Write-Host ("  {0:N1} MB" -f ((Get-Item $Zip).Length / 1MB))

Expand-Archive $Zip $Unzip -Force

# The archive contains a single top folder whose name encodes the branch.
# Locate it by content rather than by guessing the name.
$root = Get-ChildItem $Unzip -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName "pyproject.toml") } |
        Select-Object -First 1
if (-not $root) {
    throw "No project folder inside the archive. Nothing copied; your install is untouched."
}
Write-Host "  extracted: $($root.Name)"

# -- copy -------------------------------------------------------------------
# /E   include subdirectories, empty ones too
# /NFL /NDL /NJH /NJS  quiet: no per-file or per-directory listing
# Data, .env and the virtual environment are excluded: they are yours, not the
# repository's, and a copy that overwrote them would cost real work.
Write-Host "`nCopying..."
$null = robocopy $root.FullName $Project /E /NFL /NDL /NJH /NJS /XD ".venv" "data" ".git" /XF ".env"
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with code $LASTEXITCODE. Your install may be partially updated."
}
Write-Host "  done (robocopy code $LASTEXITCODE, anything under 8 is success)"

Remove-Item $Zip -Force
Remove-Item $Unzip -Recurse -Force

# -- verify -----------------------------------------------------------------
if (-not (Test-Path $exe)) {
    Write-Host "`nNo virtual environment found. Run setup-windows.bat once, then re-run this."
    exit 0
}

Write-Host "`nVerifying..."
$info = & $exe version | ConvertFrom-Json
Write-Host "  software   : $before -> $($info.software_version)"
Write-Host "  schema     : $($info.schema_version)"
Write-Host "  normalizer : $($info.normalizer_version)"

$commands = (& $exe --help | Out-String)
$missing = @("packs", "benchmark", "validate", "collect") | Where-Object { $commands -notmatch $_ }
if ($missing) {
    Write-Host "`nMISSING COMMANDS: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "The copy did not take effect. Do not re-run blindly -- send this output on."
    exit 1
}

Write-Host "`nUpdate complete. All expected commands are present." -ForegroundColor Green
