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

# The desktop client is a window onto the server, offered for download from the
# sign-in page and Settings. Its zip rides along in downloads\ beside the exe.
Say "Building the desktop client"
python -m PyInstaller --noconfirm --distpath "$stage\client-dist" --workpath "$stage\client-build" MusicClient.spec
if ($LASTEXITCODE -ne 0) { Write-Host "Client build failed." -ForegroundColor Red; exit 1 }
Remove-Item "$stage\MusicClient.zip" -ErrorAction SilentlyContinue
# python's zipfile rather than Compress-Archive: 5.1 writes backslashes into
# the entry names, which other unzip tools then mishandle.
Push-Location "$stage\client-dist"
python -m zipfile -c "$stage\MusicClient.zip" MusicClient
$zipped = $LASTEXITCODE
Pop-Location
if ($zipped -ne 0) { Write-Host "Couldn't zip the client." -ForegroundColor Red; exit 1 }

function Get-MusicServerProcess {
    # Name-based on purpose: a copy launched by the boot-time task runs in
    # the services session, and its ExecutablePath comes back empty from
    # here, so a path-only match would miss it entirely.
    @(Get-CimInstance Win32_Process -Filter "Name='MusicRequestServer.exe'" -ErrorAction SilentlyContinue)
}

function Stop-MusicServerInstances {
    # The boot task's copy is owned by the Task Scheduler in session 0
    # (S4U logon); a plain Stop-Process from a normal user session is
    # denied, but the scheduler can end its own task, so ask it first.
    # Copies the user started in their own session still need the plain kill.
    $taskName = "MusicRequestServer-BeforeSignIn"
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
    @(Get-MusicServerProcess) | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# MusicRequestServer.exe is this app's own name, so any live instance is
# holding files we are about to mirror, where it runs from. A file-share
# probe cannot find that (a running image still opens for reading; only
# overwriting it fails, which is exactly what the mirror must do), so the
# process list is the test.
$running = @(Get-MusicServerProcess)
if ($running) {
    Say "Closing the running copy"
    foreach ($attempt in 1, 2) {
        Stop-MusicServerInstances
        Start-Sleep -Milliseconds 1500
    }
}
if (@(Get-MusicServerProcess)) {
    Write-Host "The installed copy did not close; refusing to overwrite its files." -ForegroundColor Red
    exit 1
}

Say "Installing into dist\MusicRequestServer"
# /R:3 /W:5: a file that is still locked should fail fast with a clear
# error, not sit in robocopy's default 100,000 retries for hours.
robocopy "$stage\dist\MusicRequestServer" $dst /MIR /NFL /NDL /NJH /NJS /NP /R:3 /W:5 | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "Install failed (robocopy $LASTEXITCODE)." -ForegroundColor Red; exit 1 }
# After the mirror, which would otherwise delete it.
New-Item -ItemType Directory -Force "$dst\downloads" | Out-Null
Copy-Item "$stage\MusicClient.zip" "$dst\downloads\MusicClient.zip" -Force
# PyInstaller puts data files under _internal in a one-dir build. The Dynu
# certificate helper is an operator-facing command, so it must be visibly
# beside the executable rather than technically present but undiscoverable in
# the runtime internals.
# The nightly renewal task runs the copy here, and so does the app's own
# certificate keeper, so both scripts have to be here after every install.
foreach ($script in "certificate.ps1", "Request-MusicCertificate.ps1") {
    Copy-Item (Join-Path $root $script) "$dst\$script" -Force
    if (-not (Test-Path -LiteralPath "$dst\$script")) {
        Write-Host "Couldn't install $script." -ForegroundColor Red
        exit 1
    }
}

if ($NoRestart) {
    Say "Done. Start it yourself when you're ready:"
    Write-Host "    $exe"
    exit 0
}

Say "Starting"
Start-Process $exe
Start-Sleep -Seconds 6
if (@(Get-MusicServerProcess)) {
    Say "Running."
    # Explicit, or the script inherits robocopy's exit code — which is 1 for
    # "copied some files", i.e. every successful install.
    exit 0
} else {
    Write-Host "It didn't stay up - check the log:" -ForegroundColor Yellow
    Write-Host "    $env:LOCALAPPDATA\MusicRequestServer\server.log"
    exit 1
}
