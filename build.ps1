# Build Music Request Server and install it over dist\MusicRequestServer.
#
#   .\build.ps1              build, install, restart
#   .\build.ps1 -NoRestart   build and install, leave it closed
#   .\build.ps1 -CheckOnly   just run the access checks
#
# PyInstaller builds into a scratch folder rather than dist\ directly,
# because a running exe holds a lock on its own folder and the build fails
# half way through with a permission error. Robocopy /MIR then mirrors it
# across once the app is closed.

param(
    [switch]$NoRestart,
    [switch]$CheckOnly,
    [switch]$SkipChecks
)

# Not "Stop": python and pyinstaller both write ordinary progress and
# deprecation notices to stderr, and under Stop PowerShell turns the first
# line of that into a terminating NativeCommandError. Exit codes are what
# actually say whether they worked, and every call below checks one.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$dst   = Join-Path $root "dist\MusicRequestServer"
$stage = Join-Path $env:TEMP "mrs-build"
$exe   = Join-Path $dst "MusicRequestServer.exe"

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if (-not $SkipChecks) {
    Say "Running the access checks"
    $env:PYTHONIOENCODING = "utf-8"
    python launcher.pyw --check
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Checks failed - not building." -ForegroundColor Red
        exit 1
    }
}
if ($CheckOnly) { exit 0 }

Say "Building"
python -m PyInstaller --noconfirm --distpath "$stage\dist" --workpath "$stage\build" MusicRequestServer.spec
if ($LASTEXITCODE -ne 0) { Write-Host "Build failed." -ForegroundColor Red; exit 1 }

$running = Get-Process MusicRequestServer -ErrorAction SilentlyContinue
if ($running) {
    Say "Closing the running copy"
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 1500
}

Say "Installing into dist\MusicRequestServer"
robocopy "$stage\dist\MusicRequestServer" $dst /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "Install failed (robocopy $LASTEXITCODE)." -ForegroundColor Red; exit 1 }

if ($NoRestart) {
    Say "Done. Start it yourself when you're ready:"
    Write-Host "    $exe"
    exit 0
}

Say "Starting"
Start-Process $exe
Start-Sleep -Seconds 6
if (Get-Process MusicRequestServer -ErrorAction SilentlyContinue) {
    Say "Running."
    # Explicit, or the script inherits robocopy's exit code — which is 1 for
    # "copied some files", i.e. every successful install.
    exit 0
} else {
    Write-Host "It didn't stay up - check the log:" -ForegroundColor Yellow
    Write-Host "    $env:LOCALAPPDATA\MusicRequestServer\server.log"
    exit 1
}
