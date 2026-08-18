<#
.SYNOPSIS
    One-shot setup for Music Request Server: installs the prerequisites and
    gets YouTube playback working.

.DESCRIPTION
    Installs mpv, yt-dlp and Node.js via winget (skipping whatever is already
    present), installs the Python packages when running from source, creates the
    config, sets up a logged-in YouTube session for yt-dlp, then proves the whole
    chain works by downloading a real track.

    Everything it prints is also written to setup-log.txt next to this script.

.PARAMETER SkipCookies
    Install the tools only; leave the cookie configuration alone.

.PARAMETER CookieBrowser
    Force a specific browser for --cookies-from-browser instead of auto-detecting.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipCookies,
    [ValidateSet("chrome","edge","firefox","brave","vivaldi","opera","chromium")]
    [string]$CookieBrowser
)

# Native exes (pip, yt-dlp, winget) write progress to stderr. With EAP=Stop that
# becomes a terminating NativeCommandError and kills the script.
$ErrorActionPreference = "Continue"

$root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$LOG       = Join-Path $root "setup-log.txt"
$COOKIEFILE = Join-Path $root "youtube_cookies.txt"
$TESTVIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"   # stable, always-public

"Music Request Server setup - $(Get-Date)" | Set-Content $LOG -Encoding utf8

function Log($text) { Add-Content -Path $LOG -Value $text -Encoding utf8 }
function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan; Log ""; Log "==> $m" }
function Ok($m)   { Write-Host "    [ok] $m"  -ForegroundColor Green;  Log "    [ok] $m" }
function Info($m) { Write-Host "    $m"       -ForegroundColor Gray;   Log "    $m" }
function Warn($m) { Write-Host "    [!] $m"   -ForegroundColor Yellow; Log "    [!] $m" }
function Fail($m) { Write-Host "    [x] $m"   -ForegroundColor Red;    Log "    [x] $m" }

function Have($exe) { return [bool](Get-Command $exe -ErrorAction SilentlyContinue) }

# winget updates PATH but this session won't see it until we reload.
function Reload-Path {
    $m = [Environment]::GetEnvironmentVariable("Path","Machine")
    $u = [Environment]::GetEnvironmentVariable("Path","User")
    $env:Path = "$m;$u"
}

function Install-Tool($exe, $id, $label) {
    if (Have $exe) { Ok "$label already installed"; return $true }
    if (-not (Have "winget")) {
        Fail "$label is missing and winget is unavailable - install it manually."
        return $false
    }
    Info "installing $label ($id)..."
    $o = winget install --id $id --exact --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-String
    Log $o
    Reload-Path
    if (Have $exe) { Ok "$label installed"; return $true }
    Warn "$label is not on PATH yet - open a new terminal and re-run this script."
    return $false
}

# Run a yt-dlp check and classify the outcome rather than just pass/fail.
function Invoke-YtdlpCheck([string[]]$authArgs) {
    $a = @("--simulate","--no-warnings","--no-playlist","--print","%(id)s") + $authArgs + @($TESTVIDEO)
    $out = & yt-dlp @a 2>&1 | Out-String
    Log "yt-dlp $($a -join ' ')"
    Log $out
    if ($LASTEXITCODE -eq 0 -and $out -match "dQw4w9WgXcQ") { return @{ result="ok"; text=$out } }
    if ($out -match "Could not copy|another process|database is locked") { return @{ result="locked"; text=$out } }
    if ($out -match "DPAPI|App-Bound|Failed to decrypt")                 { return @{ result="unsupported"; text=$out } }
    if ($out -match "not a bot|page needs to be reloaded|Sign in")       { return @{ result="needsauth"; text=$out } }
    return @{ result="fail"; text=$out }
}

try {

# -- 1. tools ---------------------------------------------------------
Step "Prerequisites"
$null   = Install-Tool "mpv"    "shinchiro.mpv"     "mpv"
$null   = Install-Tool "yt-dlp" "yt-dlp.yt-dlp"     "yt-dlp"
$okNode = Install-Tool "node"   "OpenJS.NodeJS.LTS" "Node.js"
if (-not $okNode) { Warn "Without Node, YouTube returns no audio formats at all." }

# -- 2. python packages (source runs only; the .exe bundles them) -----
Step "Python packages"
$req = Join-Path $root "requirements.txt"
if (-not (Test-Path $req)) {
    Info "no requirements.txt here - skipping (this is the packaged .exe)"
} elseif (-not (Have "python")) {
    Warn "Python not found - only needed to run from source; the .exe bundles it."
} else {
    $py = (Get-Command python).Source
    Info "using $py"
    $pipOut = & $py -m pip install --quiet -r $req 2>&1 | Out-String
    Log $pipOut
    if ($LASTEXITCODE -eq 0) {
        Ok "packages installed"
        if ($pipOut -match "dependency conflicts") { Info "(pip noted conflicts from other packages sharing this Python)" }
    } else {
        Warn "pip reported errors:"
        ($pipOut -split "`n" | Select-Object -Last 4) | ForEach-Object { Info $_.Trim() }
    }
}

# -- 3. config --------------------------------------------------------
Step "Config"
$srcDir   = Join-Path $root "src"
$srcCfg   = Join-Path $srcDir "config.json"
$example  = Join-Path $root "config.example.json"
$localDir = Join-Path $env:LOCALAPPDATA "MusicRequestServer"
$localCfg = Join-Path $localDir "config.json"

# Source checkout: seed src\config.json from the example.
if ((Test-Path $srcDir) -and (Test-Path $example) -and -not (Test-Path $srcCfg)) {
    Copy-Item $example $srcCfg
    Ok "created src\config.json from the example"
}

# Packaged .exe: it reads %LOCALAPPDATA%\MusicRequestServer\config.json, which
# does not exist until first run - create it now so this script can configure it.
if (-not (Test-Path $srcCfg) -and -not (Test-Path $localCfg)) {
    if (-not (Test-Path $localDir)) { New-Item -ItemType Directory -Path $localDir -Force | Out-Null }
    if (Test-Path $example) {
        Copy-Item $example $localCfg
        Ok "created $localCfg from the example"
    } else {
        # Minimal config; the app generates api_key on first run.
        $seed = [ordered]@{
            host = "0.0.0.0"; port = 5000; api_key = ""
            js_runtime = "node"; player_client = "tv"
            cookies_file = ""; cookies_from_browser = ""
        }
        $json = $seed | ConvertTo-Json -Depth 5
        [System.IO.File]::WriteAllText($localCfg, $json, (New-Object System.Text.UTF8Encoding($false)))
        Ok "created $localCfg"
    }
}

$script:targets = @($srcCfg, $localCfg) | Where-Object { Test-Path $_ }
if (-not $script:targets) { Warn "no config found or created - settings cannot be saved" }

function Set-ConfigValues([hashtable]$values) {
    foreach ($path in $script:targets) {
        try {
            # -Raw keeps a BOM out of the way; ConvertFrom-Json tolerates it.
            $cfg = Get-Content $path -Raw | ConvertFrom-Json
        } catch {
            Warn "could not read $path - $($_.Exception.Message)"
            continue
        }
        foreach ($k in $values.Keys) {
            $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $values[$k] -Force
        }
        # PS 5.1's -Encoding utf8 writes a BOM and Python's json.load rejects it.
        $json = $cfg | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
        Info "updated $path"
    }
}

$base = @{ js_runtime = "node"; player_client = "tv" }
if (Have "python") { $base["python_path"] = (Get-Command python).Source }
Set-ConfigValues $base
Ok "runtime settings written (js_runtime=node, player_client=tv)"

# -- 4. cookies -------------------------------------------------------
if ($SkipCookies) {
    Step "YouTube cookies"
    Info "skipped (-SkipCookies)"
} else {
    Step "YouTube cookies"
    Info "YouTube refuses anonymous downloads, so yt-dlp needs a logged-in session."
    $solved = $false

    # 4a. An existing cookies file is the most reliable route - test it first.
    if (Test-Path $COOKIEFILE) {
        Info "found youtube_cookies.txt - testing it..."
        $r = Invoke-YtdlpCheck @("--cookies", $COOKIEFILE)
        if ($r.result -eq "ok") {
            Set-ConfigValues @{ cookies_file = $COOKIEFILE; cookies_from_browser = "" }
            Ok "cookies file works - configured cookies_file"
            $solved = $true
        } else {
            Warn "the existing cookies file did not work ($($r.result)) - it has probably expired"
        }
    }

    # 4b. Try reading cookies straight out of a browser.
    if (-not $solved) {
        $browserExes = [ordered]@{
            firefox = @("$env:ProgramFiles\Mozilla Firefox\firefox.exe", "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe")
            chrome  = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe")
            edge    = @("$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe")
            brave   = @("$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe")
            vivaldi = @("$env:LOCALAPPDATA\Vivaldi\Application\vivaldi.exe")
            opera   = @("$env:LOCALAPPDATA\Programs\Opera\opera.exe")
        }
        $procNames = @{ chrome="chrome"; edge="msedge"; firefox="firefox"; brave="brave"; vivaldi="vivaldi"; opera="opera" }

        $order = @()
        foreach ($b in $browserExes.Keys) {
            $hit = $browserExes[$b] | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
            if ($hit) { $order += $b }
        }
        if ($CookieBrowser) { $order = @($CookieBrowser) }
        if ($order.Count -gt 0) { Info ("browsers found: " + ($order -join ", ")) } else { Info "no supported browser found" }

        foreach ($b in $order) {
            Info "trying --cookies-from-browser $b ..."
            $r = Invoke-YtdlpCheck @("--cookies-from-browser", $b)

            if ($r.result -eq "locked") {
                $running = @(Get-Process -Name $procNames[$b] -ErrorAction SilentlyContinue).Count
                if ($running -gt 0) {
                    Warn "$b is running, which locks its cookie database."
                    $ans = Read-Host "    Close $b completely and retry? [Y/n]"
                    if ($ans -eq "" -or $ans -match "^[Yy]") {
                        Read-Host "    Close every $b window, then press Enter"
                        $r = Invoke-YtdlpCheck @("--cookies-from-browser", $b)
                    }
                }
            }

            switch ($r.result) {
                "ok" {
                    Set-ConfigValues @{ cookies_from_browser = $b; cookies_file = "" }
                    Ok "$b cookies work - configured cookies_from_browser = $b"
                    Info "Read live at each download, so nothing expires on disk."
                    $solved = $true
                }
                "locked"      { Warn "$b cookie database still locked" }
                "unsupported" { Warn "$b encrypts its cookies (Chrome v127+) - yt-dlp cannot read them" }
                "needsauth"   { Warn "$b cookies were read but are not logged into YouTube" }
                default       { Warn "$b cookie extraction failed" }
            }
            if ($solved) { break }
        }
    }

    # 4c. Nothing worked - walk them through the file method, precisely.
    if (-not $solved) {
        Warn "Could not set cookies up automatically."
        Info ""
        Info "Do this - it is the reliable method and takes a minute:"
        Info ""
        Info "  1. Install this browser extension:"
        Info "       Get cookies.txt LOCALLY   (Chrome/Edge/Firefox web store)"
        Info "       It exports locally - nothing is uploaded anywhere."
        Info "  2. Open a PRIVATE / INCOGNITO window and go to youtube.com."
        Info "     Sign in if you are not already."
        Info "     (Private-window cookies are not rotated, so they last far longer.)"
        Info "  3. Click the extension icon, then Export / Export As -> Netscape format."
        Info "  4. Save the file EXACTLY here, with EXACTLY this name:"
        Info ""
        Info "       $COOKIEFILE"
        Info ""
        Info "     The filename must be  youtube_cookies.txt  and it must sit in the"
        Info "     same folder as this setup script."
        Info "  5. Re-run this script. It will find the file, test it, and finish."
        Info ""

        # Leave a clearly-labelled placeholder so the target is unmistakable.
        if (-not (Test-Path $COOKIEFILE)) {
            $tpl = @(
                "# Netscape HTTP Cookie File",
                "#",
                "# THIS IS A PLACEHOLDER - IT CONTAINS NO COOKIES AND WILL NOT WORK.",
                "#",
                "# Replace this whole file with a real export:",
                "#   1. Install the 'Get cookies.txt LOCALLY' browser extension",
                "#   2. Open a private/incognito window on youtube.com, signed in",
                "#   3. Export in Netscape format",
                "#   4. Overwrite this file, keeping the name youtube_cookies.txt",
                "#   5. Re-run setup.ps1",
                "#",
                "# Expected format - one tab-separated cookie per line, like:",
                "# .youtube.com`tTRUE`t/`tTRUE`t1799999999`tSID`tsome-value-here"
            ) -join "`r`n"
            [System.IO.File]::WriteAllText($COOKIEFILE, $tpl, (New-Object System.Text.UTF8Encoding($false)))
            Info "Created a placeholder at that exact path for you to overwrite."
        }
        try {
            Start-Process explorer.exe "/select,`"$COOKIEFILE`"" -ErrorAction SilentlyContinue
            Info "(opened the folder for you)"
        } catch { }
    }
}

# -- 5. verify --------------------------------------------------------
Step "Verify"
$allGood = $true
foreach ($t in @(@("mpv","mpv"), @("yt-dlp","yt-dlp"), @("node","Node.js"))) {
    if (Have $t[0]) { Ok "$($t[1]) on PATH" } else { Fail "$($t[1]) MISSING"; $allGood = $false }
}

if (Have "yt-dlp") {
    Info "downloading a real track to prove the whole chain works..."
    $authArgs = @()
    $cfgPath = $script:targets | Select-Object -First 1
    if ($cfgPath) {
        $c = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($c.cookies_from_browser) { $authArgs += @("--cookies-from-browser", $c.cookies_from_browser) }
        elseif ($c.cookies_file -and (Test-Path $c.cookies_file)) { $authArgs += @("--cookies", $c.cookies_file) }
        if ($c.js_runtime)    { $authArgs += @("--js-runtimes", $c.js_runtime) }
        if ($c.player_client) { $authArgs += @("--extractor-args", "youtube:player_client=$($c.player_client)") }
    }
    $tmp = Join-Path $env:TEMP "mrs_setup_check"
    $dl = @("-f","bestaudio/best","--no-part","--no-warnings","-o","$tmp.%(ext)s") + $authArgs + @($TESTVIDEO)
    $out = & yt-dlp @dl 2>&1 | Out-String
    Log $out
    $got = Get-ChildItem "$tmp.*" -ErrorAction SilentlyContinue
    if ($got) {
        Ok ("download works ({0:N1} MB)" -f ($got[0].Length / 1MB))
        $got | Remove-Item -Force -ErrorAction SilentlyContinue
    } else {
        $allGood = $false
        Fail "download failed:"
        ($out -split "`n" | Where-Object { $_ -match "ERROR" } | Select-Object -Last 2) | ForEach-Object { Info $_.Trim() }
        Info "This is almost always cookies - see the cookie step above."
    }
}

Write-Host ""
if ($allGood) {
    Write-Host "Setup complete. Run MusicRequestServer.exe (or launcher.pyw from source)." -ForegroundColor Green
} else {
    Write-Host "Setup finished with problems - see the [x] lines above." -ForegroundColor Yellow
    Write-Host "Full log: $LOG" -ForegroundColor Yellow
}

} catch {
    Write-Host ""
    Fail "Setup hit an unexpected error:"
    Info $_.Exception.Message
    Info "at line $($_.InvocationInfo.ScriptLineNumber)"
    Log ($_ | Out-String)
    Write-Host ""
    Write-Host "Send me $LOG and I can see exactly what happened." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Log written to $LOG" -ForegroundColor DarkGray
if ($Host.Name -eq "ConsoleHost" -and -not $env:MRS_NO_PAUSE) {
    Write-Host "Press Enter to close..." -ForegroundColor DarkGray
    Read-Host | Out-Null
}
