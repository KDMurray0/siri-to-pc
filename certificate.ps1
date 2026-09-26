# A real certificate for this server, and a job that keeps it renewed.
#
#   .\certificate.ps1 -ClientId xxxx
#   .\certificate.ps1 -Domain music.example.dynu.net -ClientId xxxx -Staging
#   .\certificate.ps1 -Renew        # what the scheduled task and the app run
#
# -Renew only ever touches THIS server's name. The Posh-ACME store belongs to
# the Windows user, not to this app, and the movie server keeps its orders in
# the same one: renewing -AllOrders published whichever certificate renewed
# last into this server's folder, so one night it would have served the movie
# server's name. A shared Dynu domain has a 50-a-week limit that
# thousands of other people spend; renewing the same name is exempt from it,
# a new name is not, so this never asks for anything but the same name again.
#
# Let's Encrypt proves the name is yours by asking for a DNS record, so
# nothing has to be reachable from the internet while this runs and no port
# has to be open. Dynu's API writes that record; the credentials are its API
# OAuth2 pair (Dynu control panel -> API Credentials), not the account
# password, and they are only ever handed to Posh-ACME.
#
# The files land in %LOCALAPPDATA%\MusicRequestServer\certs, which is where
# the server looks. A renewal overwrites them and the server picks the new
# one up on its own, without dropping what's playing.

param(
    [string]$Domain,
    [string]$ClientId,
    [string]$Secret,
    [string[]]$AlsoCover = @(),
    [string]$Email,
    [switch]$Staging,
    [switch]$Renew,
    [switch]$NoTask
)

$ErrorActionPreference = "Continue"
$certs = Join-Path $env:LOCALAPPDATA "MusicRequestServer\certs"
$script:MainName = $Domain

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

function Install-PoshAcme {
    if (Get-Module -ListAvailable -Name Posh-ACME) { return }
    Say "Installing Posh-ACME (for this user)"
    try {
        Install-Module -Name Posh-ACME -Scope CurrentUser -Force -AllowClobber -ErrorAction Stop
    } catch {
        Fail "Couldn't install Posh-ACME: $_"
    }
}

function Configured-DynuName {
    # The normal release has only an exe and its config in LocalAppData. Read
    # just the public hostname so the operator cannot accidentally issue a
    # certificate for an example name from this script's help text. The Dynu
    # update password is deliberately never read or used here.
    $configPath = Join-Path $env:LOCALAPPDATA "MusicRequestServer\config.json"
    try {
        $settings = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json -ErrorAction Stop
        if ([string]$settings.ddns_provider -ine "dynu") { return "" }
        $name = ([string]$settings.ddns_hostname).Trim()
        if ($name -match '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$') {
            return $name
        }
    } catch { }
    return ""
}

# Copy whatever Posh-ACME just issued into the folder the server reads.
function Publish-Cert($order) {
    if (-not $order) { Fail "No certificate order to publish." }
    New-Item -ItemType Directory -Force -Path $certs | Out-Null
    $full = $order.FullChainFile
    $key = $order.KeyFile
    if (-not (Test-Path $full) -or -not (Test-Path $key)) {
        Fail "Posh-ACME did not leave a certificate at $full"
    }
    # Written beside the live files and moved into place, so the server can
    # never read a half-written pair.
    Copy-Item $full (Join-Path $certs "fullchain.pem.new") -Force
    Copy-Item $key (Join-Path $certs "privkey.pem.new") -Force
    Move-Item (Join-Path $certs "fullchain.pem.new") (Join-Path $certs "fullchain.pem") -Force
    Move-Item (Join-Path $certs "privkey.pem.new") (Join-Path $certs "privkey.pem") -Force
    Say "Certificate in place: $certs"
    $days = [math]::Round(($order.NotAfter - (Get-Date)).TotalDays)
    Say "Good for $days more days. The server takes up a new one within a minute of it being written."
}

Install-PoshAcme
Import-Module Posh-ACME -ErrorAction Stop

function Note($msg) {
    # The app reads this to know what happened and when to try again.
    $log = Join-Path $env:LOCALAPPDATA "MusicRequestServer\tls-provisioning.log"
    Add-Content -LiteralPath $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding utf8
    Say $msg
}

function Live-Names {
    $live = Join-Path $certs "fullchain.pem"
    if (-not (Test-Path $live)) { return @() }
    try {
        $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($live)
        $san = $c.Extensions | Where-Object { $_.Oid.Value -eq "2.5.29.17" }
        if ($san) { return @(($san.Format($false) -split ",\s*") | ForEach-Object { ($_ -replace "^DNS Name=", "").Trim().ToLower() }) }
        return @($c.GetNameInfo("DnsName", $false).ToLower())
    } catch { return @() }
}

if ($Renew) {
    if (-not $Domain) { $Domain = Configured-DynuName }
    if (-not $Domain) { Note "Renewal skipped: no Dynu hostname saved."; exit 0 }
    $order = Get-PAOrder -MainDomain $Domain -ErrorAction SilentlyContinue
    if (-not $order) { Note "Renewal skipped: Posh-ACME has no order for $Domain."; exit 1 }
    try {
        $done = Submit-Renewal -MainDomain $Domain -ErrorAction Stop
    } catch {
        $brief = ($_.Exception.Message -replace '[\r\n]+', ' ').Trim()
        Note ("Renewal of $Domain failed: " + $brief.Substring(0, [Math]::Min(240, $brief.Length)))
        exit 1
    }
    if ($done) {
        Publish-Cert $done
        Note "Renewed $Domain."
        exit 0
    }
    # Not due. Still make sure the live pair is this name's: anything that
    # published another order over it is undone here, the same night.
    $names = Live-Names
    $cert = Get-PACertificate -MainDomain $Domain -ErrorAction SilentlyContinue
    if ($names -notcontains $Domain.ToLower() -and $cert -and (Test-Path $cert.FullChainFile) -and (Test-Path $cert.KeyFile)) {
        Publish-Cert $cert
        Note "Live certificate was for '$($names -join ', ')'; put $Domain's back."
    } else {
        Say "Nothing was due for $Domain."
    }
    exit 0
}

if (-not $Domain) {
    $Domain = Configured-DynuName
    if ($Domain) { Say "Using the Dynu hostname saved in Music Request Server: $Domain" }
}
if (-not $Domain) { Fail "Give me the Dynu name this server answers to: -Domain music.example.dynu.net" }
# The name can be discovered from config after the initial module setup. Keep
# the final status text in step with that resolved value rather than printing
# the empty pre-resolution parameter.
$script:MainName = $Domain
if (-not $ClientId) { $ClientId = [string]$env:DYNU_CLIENT }
if (-not $ClientId) { Fail "Give me the Dynu API client id: -ClientId xxxx (or the DYNU_CLIENT environment variable)" }

if (-not $Secret) { $Secret = [string]$env:DYNU_SECRET }
if (-not $Secret) {
    $secure = Read-Host "Dynu API secret" -AsSecureString
} else {
    $secure = ConvertTo-SecureString $Secret -AsPlainText -Force
}

Set-PAServer -DirectoryUrl ($(if ($Staging) { "LE_STAGE" } else { "LE_PROD" }))
if ($Staging) {
    Say "Using Let's Encrypt's staging service -- the certificate will NOT be trusted."
    Say "It proves the DNS side works without spending one of your five-a-week real ones."
}

$names = @($Domain) + $AlsoCover
Say ("Asking for: " + ($names -join ", "))
$pArgs = @{ DynuClientID = $ClientId; DynuSecretSecure = $secure }
$params = @{
    Domain     = $names
    Plugin     = @($names | ForEach-Object { "Dynu" })
    PluginArgs = $pArgs
    AcceptTOS  = $true
    Force      = $true
}
if ($Email) { $params["Contact"] = $Email }

$order = New-PACertificate @params
if (-not $order) { Fail "No certificate was issued. The error above says why." }
Publish-Cert $order

if (-not $NoTask -and -not $Staging) {
    Say "Registering a daily renewal check"
    $me = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$me`" -Renew"
    $trigger = New-ScheduledTaskTrigger -Daily -At 3:20am
    try {
        Register-ScheduledTask -TaskName "Music Request Server certificate" -Action $action `
            -Trigger $trigger -Description "Renews the TLS certificate when it's due" `
            -User $env:USERNAME -RunLevel Limited -Force | Out-Null
        Say "Done. It checks each night; Let's Encrypt renews at 30 days left."
    } catch {
        Write-Host "Couldn't register the task: $_" -ForegroundColor Yellow
        Write-Host "Run this yourself every month or so:  .\certificate.ps1 -Renew"
    }
}

Say "Restart Music Request Server once, and it will serve https on its own port."
Write-Host "    Links will say https://$($script:MainName):<port>/player"
