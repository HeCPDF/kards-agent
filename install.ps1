# kards-agent installer (Windows PowerShell 5.1+ / PowerShell 7).
# Creates .venv next to this script, installs requirements, runs the offline test suite.
#   .\install.ps1                # main environment + offline self-check
#   .\install.ps1 -SkipTests     # install only
#   .\install.ps1 -Python "py -3.12"
# Never touches the game. Paths are relative to this script; nothing is hard-coded.
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

# 1. interpreter check (3.10+)
$parts = $Python -split ' '
$exe = $parts[0]
$pre = @(); if ($parts.Count -gt 1) { $pre = $parts[1..($parts.Count - 1)] }
$ver = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0) { throw "Python not found: $Python" }
if ([version]$ver -lt [version]"3.10") { throw "Python 3.10+ required, found $ver" }
Write-Host "Using Python $ver"

# 2. main venv
$venv = Join-Path $Root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating $venv"
    Invoke-Native $exe ($pre + @("-m", "venv", $venv))
}
Invoke-Native $py @("-m", "pip", "install", "--upgrade", "pip")
# requirements-dev.txt = requirements.txt + the one extra package the test-suite needs
$reqFile = "requirements.txt"
if (-not $SkipTests) { $reqFile = "requirements-dev.txt" }
Invoke-Native $py @("-m", "pip", "install", "-r", (Join-Path $Root $reqFile))

# 3. offline self-check (no game needed)
if (-not $SkipTests) {
    Write-Host "Running offline tests (tests\run_all.py) ..."
    Invoke-Native $py @((Join-Path $Root "tests\run_all.py"))
}

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  python -m gui.app  control panel"
Write-Host "  run_listener.bat   resident listener (start the game first)"
Write-Host "  run_tests.bat      offline tests"
