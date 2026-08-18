<#
.SYNOPSIS
    One-shot setup for Music Request Server: installs the prerequisites and
    configures YouTube cookies.

.DESCRIPTION
    Installs mpv, yt-dlp and Node.js via winget (skipping whatever is already
    present), installs the Python packages when running from source, then works
    out how to give yt-dlp a logged-in YouTube session - trying
    --cookies-from-browser against each installed browser and falling back to an
    exported cookies.txt when the browser cannot be read.

.PARAMETER SkipCookies
    Install the tools only; leave the cookie configuration alone.

.PARAMETER CookieBrowser
    Force a specific browser instead of auto-detecting.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipCookies,
    [ValidateSet("chrome","edge","firefox","brave","vivaldi","opera","chromium")]
    [string]$CookieBrowser
)

$ErrorActionPreference = "Continue"   # native exes write to stderr; do not treat that as fatal
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$TESTVIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"   # stable, always-public

function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [ok] $m" -ForegroundColor Green }
function Info($m) { Write-Host "    $m" -ForegroundColor Gray }
function Warn($m) { Write-Host "    [!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "    [x] $m" -ForegroundColor Red }

function Have($exe) {
    return [bool](Get-Command $exe -ErrorAction SilentlyContinue)
}

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
    winget install --id $id --exact --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
    Reload-Path
    if (Have $exe) { Ok "$label installed"; return $true }
    Warn "$label is not on PATH yet - a new terminal (or reboot) may be needed."
    return $false
}

# -- 1. tools ---------------------------------------------------------
Step "Prerequisites"
$null   = Install-Tool "mpv"    "shinchiro.mpv"     "mpv"
$null   = Install-Tool "yt-dlp" "yt-dlp.yt-dlp"     "yt-dlp"
$okNode = Install-Tool "node"   "OpenJS.NodeJS.LTS" "Node.js"
if (-not $okNode) { Warn "Without Node, YouTube returns no audio formats." }

# -- 2. python packages (source runs only; the .exe bundles them) -----
Step "Python packages"
$req = Join-Path $root "requirements.txt"
if (-not (Test-Path $req)) {
    Info "no requirements.txt here - skipping (running from the packaged .exe?)"
} elseif (-not (Have "python")) {
    Warn "Python not found - only needed to run from source; the .exe bundles it."
} else {
    $py = (Get-Command python).Source
    Info "using $py"
    $pipOut = & $py -m pip install --quiet -r $req 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Ok "packages installed"
        # pip warns about unrelated projects sharing this interpreter; not ours to fix.
        if ($pipOut -match "dependency conflicts") { Info "(pip noted pre-existing conflicts from other packages)" }
    } else {
        Warn "pip reported errors:"
        ($pipOut -split "`n" | Select-Object -Last 4) | ForEach-Object { Info $_.Trim() }
    }
}

# -- 3. config --------------------------------------------------------
Step "Config"
$srcCfg   = Join-Path $root "src\config.json"
$example  = Join-Path $root "config.example.json"
$localCfg = Join-Path $env:LOCALAPPDATA "MusicRequestServer\config.json"

if ((Test-Path $example) -and -not (Test-Path $srcCfg)) {
    Copy-Item $example $srcCfg
    Ok "created src\config.json from the example"
}

# Update every config the app might read: the source dir and the .exe data dir.
$script:targets = @($srcCfg, $localCfg) | Where-Object { Test-Path $_ }
if (-not $script:targets) { Info "no config yet - one is generated on first run" }

function Set-ConfigValues([hashtable]$values) {
    foreach ($path in $script:targets) {
        try {
            $cfg = Get-Content $path -Raw | ConvertFrom-Json
        } catch {
            Warn "could not read $path"
            continue
        }
        foreach ($k in $values.Keys) {
            $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $values[$k] -Force
        }
        # PS 5.1's -Encoding utf8 writes a BOM and Python's json.load chokes on
        # it, so write UTF-8 without one.
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
    Info "YouTube blocks anonymous downloads, so yt-dlp needs a logged-in session."

    $browserExes = [ordered]@{
        chrome  = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe")
        edge    = @("$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe")
        firefox = @("$env:ProgramFiles\Mozilla Firefox\firefox.exe", "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe")
        brave   = @("$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe")
        vivaldi = @("$env:LOCALAPPDATA\Vivaldi\Application\vivaldi.exe")
        opera   = @("$env:LOCALAPPDATA\Programs\Opera\opera.exe")
    }
    $procNames = @{ chrome="chrome"; edge="msedge"; firefox="firefox"; brave="brave"; vivaldi="vivaldi"; opera="opera" }

    $installed = @()
    foreach ($b in $browserExes.Keys) {
        $hit = $browserExes[$b] | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if ($hit) { $installed += $b }
    }
    if ($CookieBrowser) { $installed = @($CookieBrowser) }
    if ($installed.Count -gt 0) {
        Info ("browsers found: " + ($installed -join ", "))
    } else {
        Info "browsers found: none"
    }

    # Firefox first - Chromium browsers encrypt cookies in a way yt-dlp often
    # cannot read (Chrome v127+ App-Bound Encryption).
    $order = @()
    foreach ($p in @("firefox","chrome","edge","brave","vivaldi","opera")) {
        if ($installed -contains $p) { $order += $p }
    }

    function Test-BrowserCookies($b) {
        # ok | locked | unsupported | fail
        $out = & yt-dlp --cookies-from-browser $b --simulate --no-warnings --no-playlist --print "%(id)s" $TESTVIDEO 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0 -and $out -match "dQw4w9WgXcQ") { return "ok" }
        if ($out -match "Could not copy|another process|database is locked") { return "locked" }
        if ($out -match "DPAPI|App-Bound|decrypt") { return "unsupported" }
        return "fail"
    }

    $chosen = $null
    foreach ($b in $order) {
        Info "testing --cookies-from-browser $b ..."
        $r = Test-BrowserCookies $b
        if ($r -eq "locked") {
            $running = @(Get-Process -Name $procNames[$b] -ErrorAction SilentlyContinue).Count
            if ($running -gt 0) {
                Warn "$b is running, which locks its cookie database."
                $ans = Read-Host "    Close $b and retry? [Y/n]"
                if ($ans -eq "" -or $ans -match "^[Yy]") {
                    Read-Host "    Close all $b windows, then press Enter"
                    $r = Test-BrowserCookies $b
                }
            }
        }
        switch ($r) {
            "ok"          { Ok "$b cookies work"; $chosen = $b }
            "locked"      { Warn "$b cookie database still locked" }
            "unsupported" { Warn "$b encrypts its cookies (Chrome v127+) - yt-dlp cannot read them" }
            default       { Warn "$b cookie extraction failed" }
        }
        if ($chosen) { break }
    }

    if ($chosen) {
        Set-ConfigValues @{ cookies_from_browser = $chosen; cookies_file = "" }
        Ok "configured cookies_from_browser = $chosen"
        Info "Cookies are read live on each download, so nothing expires on disk."
    } else {
        Warn "No browser could be read automatically."
        Info ""
        Info "Two ways forward:"
        Info "  1. Install Firefox, log into YouTube there, then re-run this script:"
        Info "       winget install --id Mozilla.Firefox -e"
        Info "  2. Export a cookies file:"
        Info "       - install the 'Get cookies.txt LOCALLY' browser extension"
        Info "       - open a private window, go to youtube.com, confirm you are logged in"
        Info "       - export (Netscape format) to: $root\youtube_cookies.txt"
        Info "       - re-run this script and it will pick the file up"
        $cf = Join-Path $root "youtube_cookies.txt"
        if (Test-Path $cf) {
            Set-ConfigValues @{ cookies_file = $cf }
            Ok "found youtube_cookies.txt - configured cookies_file"
        }
    }
}

# -- 5. verify --------------------------------------------------------
Step "Verify"
$allGood = $true
foreach ($t in @(@("mpv","mpv"), @("yt-dlp","yt-dlp"), @("node","Node.js"))) {
    if (Have $t[0]) { Ok "$($t[1]) on PATH" } else { Fail "$($t[1]) MISSING"; $allGood = $false }
}

if (Have "yt-dlp") {
    Info "testing a real YouTube fetch..."
    $ytArgs = @("--simulate","--no-warnings","--no-playlist","--print","%(title)s")
    $cfgPath = $script:targets | Select-Object -First 1
    if ($cfgPath) {
        $c = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($c.cookies_from_browser) {
            $ytArgs += @("--cookies-from-browser", $c.cookies_from_browser)
        } elseif ($c.cookies_file -and (Test-Path $c.cookies_file)) {
            $ytArgs += @("--cookies", $c.cookies_file)
        }
        if ($c.js_runtime)    { $ytArgs += @("--js-runtimes", $c.js_runtime) }
        if ($c.player_client) { $ytArgs += @("--extractor-args", "youtube:player_client=$($c.player_client)") }
    }
    $out = & yt-dlp @ytArgs $TESTVIDEO 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Ok ("YouTube fetch works: " + $out.Trim())
    } else {
        $allGood = $false
        Fail "YouTube fetch failed:"
        ($out -split "`n" | Select-Object -Last 3) | ForEach-Object { Info $_.Trim() }
        Info "This is almost always cookies - see the cookie step above."
    }
}

Write-Host ""
if ($allGood) {
    Write-Host "Setup complete. Run MusicRequestServer.exe, or launcher.pyw from source." -ForegroundColor Green
} else {
    Write-Host "Setup finished with warnings - see the [x] lines above." -ForegroundColor Yellow
}
