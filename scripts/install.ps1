# Install or update Live Clipper on Windows. Safe to run again.
# Checks Python 3.11+, ffmpeg and pipx, asks before installing anything, then installs the app.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Yes] [-NoStart]
param([switch]$Yes, [switch]$NoStart)
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:LIVE_CLIPPER_REPO) { $env:LIVE_CLIPPER_REPO } else { 'https://github.com/OWNER/live-clipper' }

function Ask([string]$Question) {
    if ($Yes) { return $true }
    return (Read-Host "$Question [y/N]") -match '^(y|yes)$'
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Find-Python {
    foreach ($cmd in @('py -3', 'python', 'python3')) {
        $parts = $cmd -split ' '
        if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
        $extra = @($parts | Select-Object -Skip 1)
        & $parts[0] @extra -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>$null
        if ($LASTEXITCODE -eq 0) { return $cmd }
    }
    return $null
}

function Install-WithWinget([string]$What, [string]$Id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget is not available. Please install $What yourself, then run this script again."
        exit 1
    }
    if (Ask "Install $What with: winget install $Id ?") {
        winget install --id $Id -e --accept-package-agreements --accept-source-agreements
        Refresh-Path
    } else {
        Write-Host "Skipped. Install $What yourself, then run this script again."
        exit 1
    }
}

$py = Find-Python
if (-not $py) {
    Install-WithWinget 'Python 3.12' 'Python.Python.3.12'
    $py = Find-Python
    if (-not $py) { Write-Host 'Python 3.11 or newer is still not available. Open a new terminal and run this again.'; exit 1 }
}
$pyParts = $py -split ' '
$pyExe = $pyParts[0]
$pyArgs = @($pyParts | Select-Object -Skip 1)
Write-Host ("Python: " + (& $pyExe @pyArgs --version))

if (-not ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue))) {
    Install-WithWinget 'ffmpeg' 'Gyan.FFmpeg'
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Write-Host 'ffmpeg was installed but is not visible yet. Open a new terminal and run this script again.'
        exit 1
    }
}
Write-Host 'ffmpeg: found'

$havePipx = $null -ne (Get-Command pipx -ErrorAction SilentlyContinue)
if (-not $havePipx) {
    if (Ask "Install pipx with: $py -m pip install --user pipx ?") {
        & $pyExe @pyArgs -m pip install --user pipx
        & $pyExe @pyArgs -m pipx ensurepath
        Refresh-Path
        $havePipx = $null -ne (Get-Command pipx -ErrorAction SilentlyContinue)
    }
}

if ($havePipx) {
    pipx install --force "git+$RepoUrl"
} else {
    Write-Host 'Using pip instead of pipx.'
    & $pyExe @pyArgs -m pip install --user --upgrade "git+$RepoUrl"
    $scripts = & $pyExe @pyArgs -c 'import sysconfig; print(sysconfig.get_path("scripts", "nt_user"))'
    $env:Path = "$env:Path;$scripts"
}

if (-not (Get-Command live-clipper -ErrorAction SilentlyContinue)) {
    Write-Host "Installed, but 'live-clipper' is not on PATH yet. Open a new terminal, or run: $py -m live_clipper start"
    exit 0
}

live-clipper doctor
if (-not $NoStart) { live-clipper start } else { Write-Host 'Done. Start it with: live-clipper start' }
