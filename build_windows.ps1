# Build the Windows one-file binary into dist\minjpg.exe
#
# Run this on Windows with Python 3.12, 3.13 or 3.14 installed from python.org.
# PyInstaller cannot cross-compile, so this cannot be run from Linux.
# Step-by-step instructions, including what to do when something fails, are in
# the "Windows, step by step" section of README.md.
#
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- Checks first, so a wrong Python fails with an answer rather than a wall of
# --- pip output 30 seconds later.

# Ask each candidate for its version rather than trusting that it exists: the
# `python` on a fresh Windows PATH is often the Microsoft Store stub, which
# resolves fine, prints a nag to stderr and exits without running anything.
$python = $null
$versionText = ""
foreach ($candidate in @("python", "py")) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    $reported = (& $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $reported -match '^\s*3\.\d+\s*$') {
        $python = $candidate
        $versionText = $reported.Trim()
        break
    }
}
if (-not $python) {
    Write-Host ""
    Write-Host "No working Python was found on PATH." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/windows/ and make sure"
    Write-Host "'Add python.exe to PATH' is ticked in the installer, then open a NEW"
    Write-Host "PowerShell window and run this script again."
    Write-Host ""
    Write-Host "If typing 'python' opens the Microsoft Store, that is a placeholder rather"
    Write-Host "than a real install - use the python.org installer above."
    exit 1
}

# numpy 2.5.1 has no ready-made package below 3.12, and none of the pins have one
# for 3.15 yet, so anything outside this range would try to compile from source.
if ($versionText -notin @("3.12", "3.13", "3.14")) {
    Write-Host ""
    Write-Host "Found Python $versionText, which this build does not support." -ForegroundColor Red
    Write-Host "Install Python 3.12, 3.13 or 3.14 from python.org. Outside that range pip"
    Write-Host "has no ready-made packages and would try to compile numpy from source,"
    Write-Host "which needs a full C++ toolchain."
    exit 1
}

$arch = (& $python -c "import platform; print(platform.machine())").Trim()
if ($arch -notin @("AMD64", "x86_64")) {
    Write-Host ""
    Write-Host "This Python is $arch; the bundled MozJPEG encoder is 64-bit x86 only." -ForegroundColor Red
    Write-Host "Install the 64-bit ('Windows installer (64-bit)') build from python.org."
    exit 1
}

# The GUI is tkinter. It ships with python.org builds unless deliberately
# deselected, but not with every distribution.
& $python -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "This Python has no tkinter, so the GUI cannot be built." -ForegroundColor Red
    Write-Host "Re-run the python.org installer, choose 'Modify', and tick"
    Write-Host "'tcl/tk and IDLE'. Microsoft Store builds of Python are also known"
    Write-Host "to cause trouble here - prefer the installer from python.org."
    exit 1
}

Write-Host "Using $python $versionText ($arch)"

# --- Build

$venv = ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "Creating virtualenv in $venv"
    & $python -m venv $venv
}
$venvPython = Join-Path $venv "Scripts\python.exe"

Write-Host "Installing pinned dependencies"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt

Write-Host "Checking the vendored MozJPEG binary"
& $venvPython tools\fetch_cjpeg.py --check
if ($LASTEXITCODE -ne 0) {
    Write-Host "The vendored encoder failed its checksum check - refusing to build." -ForegroundColor Red
    exit 1
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& (Join-Path $venv "Scripts\pyinstaller.exe") --noconfirm --clean minjpg.spec

$exe = Resolve-Path "dist\minjpg.exe"
Write-Host ""
Write-Host "Built: $exe" -ForegroundColor Green
Write-Host "Check it with:  .\dist\minjpg.exe --selftest"
