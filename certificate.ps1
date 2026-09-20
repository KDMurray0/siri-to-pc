# A real certificate for this server, and a job that keeps it renewed.
#
#   .\certificate.ps1 -Domain music.example.dynu.net -ClientId xxxx
#   .\certificate.ps1 -Domain music.example.dynu.net -ClientId xxxx -Staging
#   .\certificate.ps1 -Renew        # what the scheduled task runs
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

if ($Renew) {
    # The scheduled task's path: renew anything due, then republish.
    Say "Renewing anything that's due"
    $done = Submit-Renewal -AllOrders
    if ($done) { foreach ($order in $done) { Publish-Cert $order } }
    else { Say "Nothing was due." }
    exit 0
}

if (-not $Domain) { Fail "Give me the name this server answers to: -Domain music.example.dynu.net" }
if (-not $ClientId) { Fail "Give me the Dynu API client id: -ClientId xxxx" }

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
