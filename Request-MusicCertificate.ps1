# Issue a real certificate for this server's Dynu hostname, and let the live
# server take it up without a restart.
#
# This is the one-off companion to certificate.ps1 (for a domain change or a
# first certificate). It is deliberately free of secrets: the Dynu API
# credential is taken from the encrypted copy Posh-ACME already keeps in this
# Windows user's profile (left by a previous Dynu order), so a scheduled task
# can run this safely and no provider secret ever touches a script, a
# configuration file, or a task argument.
#
# The finished pair lands in %LOCALAPPDATA%\MusicRequestServer\certs, the
# standard location the running server watches. It notices the new file stamp,
# rebuilds its TLS socket, and whatever is playing keeps playing. Nothing is
# killed here.

[CmdletBinding()]
param(
    [string]$Domain,
    [int]$WaitSeconds = 240,
    [switch]$SkipSelfCleanup
)

$ErrorActionPreference = 'Stop'
$data = Join-Path $env:LOCALAPPDATA 'MusicRequestServer'
$configFile = Join-Path $data 'config.json'
$certs = Join-Path $data 'certs'
$log = Join-Path $data 'tls-provisioning.log'
$taskName = 'Music Request Server certificate (one-off)'

function Note([string]$message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $log -Value "$stamp  $message" -Encoding utf8
    Write-Host "==> $message" -ForegroundColor Cyan
}

try {
    if (-not (Test-Path -LiteralPath $configFile)) {
        throw 'Music Request Server has not saved its settings yet.'
    }
    $saved = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
    if (-not $Domain) {
        $Domain = ([string]$saved.ddns_hostname).Trim().TrimEnd('.')
    }
    if (-not $Domain) {
        throw 'Give me the Dynu name this server answers to, or set the saved ddns_hostname.'
    }
    $port = 29543
    if ($saved.PSObject.Properties['port'] -and $saved.port) { $port = [int]$saved.port }

    Import-Module Posh-ACME -ErrorAction Stop
    Set-PAServer LE_PROD | Out-Null

    # A previous, working Dynu order keeps its client id and secure secret in
    # the current Windows user's Posh-ACME store. Reusing that stored
    # credential is safer than copying it to this script, a build, a
    # configuration file, or a task argument.
    $seed = @(Get-PAOrder | Where-Object {
        @($_.Plugin) -contains 'Dynu'
    } | Select-Object -First 1)
    if (-not $seed.Count) {
        throw 'No existing Posh-ACME Dynu order was found for this Windows user.'
    }
    $pluginArgs = Get-PAPluginArgs -MainDomain $seed[0].MainDomain -Name $seed[0].Name
    if (-not $pluginArgs -or -not $pluginArgs.ContainsKey('DynuClientID') -or
        -not $pluginArgs.ContainsKey('DynuSecretSecure')) {
        throw 'The existing Posh-ACME Dynu order has no usable encrypted Dynu credentials.'
    }

    Note "Asking Let's Encrypt for $Domain (Dynu credential from the Posh-ACME profile)."
    $certificate = New-PACertificate -Domain $Domain -Plugin Dynu `
        -PluginArgs $pluginArgs -Force -DnsSleep 30 -ValidationTimeout 300 -ErrorAction Stop
    if (-not $certificate.FullChainFile -or -not $certificate.KeyFile -or
        -not (Test-Path -LiteralPath $certificate.FullChainFile) -or
        -not (Test-Path -LiteralPath $certificate.KeyFile)) {
        throw 'The certificate authority did not return a usable certificate and key.'
    }

    # Write beside the live pair and move in, so the running server can never
    # read a half-written file. It watches the file stamp and rebuilds its own
    # socket, so there is nothing to restart.
    New-Item -ItemType Directory -Force -Path $certs | Out-Null
    Move-Item -LiteralPath $certificate.FullChainFile -Destination (Join-Path $certs 'fullchain.pem') -Force
    Move-Item -LiteralPath $certificate.KeyFile -Destination (Join-Path $certs 'privkey.pem') -Force
    Note "Certificate installed: $certs"

    # Confirm the live socket actually switched to the new name. The server
    # polls the stamp every twenty seconds, so allow a few minutes.
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $live = $false
    while (-not $live -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        $tcp = $null
        try {
            $tcp = New-Object Net.Sockets.TcpClient
            $wait = $tcp.BeginConnect('127.0.0.1', $port, $null, $null)
            if ($wait.AsyncWaitHandle.WaitOne(8000)) { $tcp.EndConnect($wait) }
            else { continue }
            $ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, { $true })
            $ssl.AuthenticateAsClient($Domain)
            # The new name sits in the served certificate (SAN, plain ASCII).
            # The old one cannot contain it, so this is a clean switch check.
            $live = [Text.Encoding]::ASCII.GetString($ssl.RemoteCertificate.GetRawCertData()).Contains($Domain)
            $ssl.Dispose()
        } catch {
            # The socket may be between rebuilds; the loop retries.
        } finally {
            if ($tcp) { $tcp.Close() }
        }
    }
    if ($live) {
        Note "Live socket is serving the $Domain certificate."
    } else {
        Note "WARNING: certificate installed, but the live socket had not switched within $WaitSeconds s. It should follow on its next rebuild; check later."
    }

    if (-not $SkipSelfCleanup) {
        $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($t) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false | Out-Null
            Note "Removed the one-off task '$taskName'."
        }
    }
    exit 0
} catch {
    # Provider failures can include request details. Persist only the compact
    # reason; do not risk putting Dynu or ACME credentials into a local log.
    $brief = ($_.Exception.Message -replace '[\r\n]+', ' ').Trim()
    Note ('Certificate setup failed: ' + $brief.Substring(0, [Math]::Min(240, $brief.Length)))
    exit 1
}
