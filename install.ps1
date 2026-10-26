# ============================================================================
# Canvas Grader - Windows installer
#
# Required:
#   - Python 3.9+ with tkinter            (installed with winget if missing)
#   - .venv + requests + playwright
#   - A browser for the Canvas login: Google Chrome or Microsoft Edge
#     (Playwright's own Chromium is installed only if neither exists)
# Optional (for AI grading; asked one by one):
#   - Java JDK (Temurin)   lets the AI compile and run students' Java code
#   - Claude Code CLI      (needs Git for Windows)
#   - Codex CLI            (needs Node.js)
#
# Usage (or just double-click install.bat):
#   powershell -ExecutionPolicy Bypass -File install.ps1            interactive
#   powershell -ExecutionPolicy Bypass -File install.ps1 -All       install every optional component
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Minimal   required components only
# ============================================================================

param(
    [switch]$All,
    [switch]$Minimal
)

$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Dir ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$App = Join-Path $Dir "canvas_grader_gui.py"

function Section($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }
function Ok($text)      { Write-Host "  [OK] $text" -ForegroundColor Green }
function Warn($text)    { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Fail($text)    { Write-Host "  [X]  $text" -ForegroundColor Red; Read-Host "Press Enter to exit"; exit 1 }
function Has($name)     { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Ask($question) {
    if ($All) { return $true }
    if ($Minimal) { return $false }
    $reply = Read-Host "  $question [y/N]"
    return $reply -match '^[Yy]'
}

function Refresh-Path {
    # Pick up PATH changes made by installers without reopening the window
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:Path"
}

function Winget-Install($id, $label) {
    if (-not (Has "winget")) {
        Warn "winget is not available. Please install $label manually."
        return $false
    }
    Write-Host "  winget install $id"
    & winget install -e --id $id --accept-package-agreements --accept-source-agreements | Out-Host
    $ok = ($LASTEXITCODE -eq 0)
    Refresh-Path
    return $ok
}

if (-not (Test-Path $App)) { Fail "canvas_grader_gui.py not found. Put install.ps1 in the same folder and run it again." }

# ----------------------------------------------------------------------------
Section "1. Python 3.9+ (with tkinter)"
$PyCheck = "import sys, tkinter, venv; assert sys.version_info >= (3, 9); tkinter.Tcl()"

function Test-Python($exe, $pyArgs) {
    try {
        & $exe @pyArgs -c $PyCheck 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    # "py" is the official launcher; "python" may be the Microsoft Store stub, which fails the test
    $candidates = @(
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        Get-ChildItem -Path $root -Filter "Python3*" -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | ForEach-Object {
                $candidates += @{ Exe = (Join-Path $_.FullName "python.exe"); Args = @() }
            }
    }
    foreach ($c in $candidates) {
        if ((Has $c.Exe) -or (Test-Path $c.Exe)) {
            if (Test-Python $c.Exe $c.Args) {
                $real = & $c.Exe @($c.Args) -c "import sys; print(sys.executable)"
                return $real.Trim()
            }
        }
    }
    return $null
}

$Py = Find-Python
if (-not $Py) {
    Warn "No Python 3.9+ with tkinter found. Installing Python 3.12 with winget..."
    if (Winget-Install "Python.Python.3.12" "Python 3.12 (https://www.python.org/downloads/)") { $Py = Find-Python }
    if (-not $Py) { Fail "Please install Python from https://www.python.org/downloads/ (tick 'tcl/tk'), then run this script again." }
}
Ok ((& $Py --version) + "  ($Py)")

# ----------------------------------------------------------------------------
Section "2. Virtual environment .venv + requests + playwright"
if ((Test-Path $VenvPy) -and -not (Test-Python $VenvPy @())) {
    Warn "Existing .venv is broken; recreating it"
    Remove-Item -Recurse -Force $Venv
}
if (-not (Test-Path $VenvPy)) {
    & $Py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the virtual environment" }
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet --upgrade requests playwright
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
Ok (& $VenvPy -c "import requests, importlib.metadata as m; print('requests', requests.__version__, '/ playwright', m.version('playwright'))")

# ----------------------------------------------------------------------------
Section "3. Browser (for the Canvas login)"
$Chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$Edge = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Chrome) {
    Ok "Google Chrome found"
} elseif ($Edge) {
    Ok "Microsoft Edge found (used for the login window)"
} else {
    Warn "Neither Chrome nor Edge found; installing Playwright's Chromium (~150 MB)"
    & $VenvPy -m playwright install chromium
    Ok "Chromium installed"
}

# ----------------------------------------------------------------------------
Section "4. Optional: tools for AI grading"

function Find-Javac {
    if (Has "javac") { return (Get-Command javac).Source }
    $roots = @("$env:ProgramFiles\Eclipse Adoptium", "$env:ProgramFiles\Java", "$env:ProgramFiles\Microsoft")
    foreach ($root in $roots) {
        $hit = Get-ChildItem -Path "$root\jdk*\bin\javac.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

if (Find-Javac) {
    Ok "Java JDK found"
} elseif (Ask "Install Java JDK (Temurin 21)? The AI needs it to compile and run student code") {
    if (Winget-Install "EclipseAdoptium.Temurin.21.JDK" "a JDK (https://adoptium.net)") { Ok "Java JDK installed" }
    else { Warn "Java installation failed; install it later from https://adoptium.net" }
} else {
    Warn "Skipped Java JDK (the AI can only read the code, not run it)"
}

# Claude Code (native Windows install needs Git for Windows)
if ((Has "claude") -or (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) {
    Ok "Claude Code found"
} elseif (Ask "Install Claude Code CLI?") {
    if (-not (Has "git")) {
        Write-Host "  Claude Code needs Git for Windows; installing it first"
        Winget-Install "Git.Git" "Git for Windows (https://git-scm.com/download/win)" | Out-Null
    }
    try {
        Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
        Refresh-Path
        Ok "Claude Code installed (run 'claude' once in a terminal to log in)"
    } catch { Warn "Claude Code installation failed: $_" }
} else {
    Warn "Skipped Claude Code"
}

# Codex CLI (needs Node.js)
if (Has "codex") {
    Ok "Codex found"
} elseif (Ask "Install Codex CLI (npm install -g @openai/codex)?") {
    if (-not (Has "npm")) { Winget-Install "OpenJS.NodeJS.LTS" "Node.js (https://nodejs.org)" | Out-Null }
    if (Has "npm") {
        & npm install -g @openai/codex | Out-Host
        if ($LASTEXITCODE -eq 0) { Ok "Codex installed (run 'codex' once in a terminal to log in)" }
        else { Warn "Codex installation failed" }
    } else {
        Warn "npm not found; install Node.js from https://nodejs.org and run this script again"
    }
} else {
    Warn "Skipped Codex"
}

# ----------------------------------------------------------------------------
Section "5. Launchers"
$Launchers = @(
    @{ Name = "Canvas Grader (中文).bat";   Script = "canvas_grader_gui.py" },
    @{ Name = "Canvas Grader (English).bat"; Script = "canvas_grader_gui_en.py" },
    @{ Name = "Canvas Grader (한국어).bat";  Script = "canvas_grader_gui_ko.py" }
)
foreach ($l in $Launchers) {
    if (-not (Test-Path (Join-Path $Dir $l.Script))) { continue }
    $bat = "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `".venv\Scripts\pythonw.exe`" $($l.Script)`r`n"
    [System.IO.File]::WriteAllText((Join-Path $Dir $l.Name), $bat, [System.Text.Encoding]::ASCII)
    Ok $l.Name
}

# ----------------------------------------------------------------------------
Section "6. Check"
$check = @"
import sys
import tkinter, requests  # noqa: F401
from playwright.sync_api import sync_playwright  # noqa: F401
compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")
print("  [OK] tkinter / requests / playwright import fine; canvas_grader_gui.py compiles")
"@
$checkFile = Join-Path $env:TEMP "canvas_grader_check.py"
[System.IO.File]::WriteAllText($checkFile, $check, (New-Object System.Text.UTF8Encoding $false))
& $VenvPy $checkFile $App
if ($LASTEXITCODE -ne 0) { Fail "Final check failed" }
Remove-Item $checkFile -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done! Double-click one of the 'Canvas Grader (...).bat' files in this folder to start." -ForegroundColor Green
if (-not $All -and -not $Minimal) { Read-Host "Press Enter to close" }
