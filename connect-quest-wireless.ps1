param(
    [string]$KnownIp = "",
    [ValidateRange(1024, 65535)]
    [int]$Port = 5555,
    [ValidateRange(50, 2000)]
    [int]$ScanTimeoutMs = 250,
    [switch]$Quiet
)

# Reconnect to an already-wireless Quest without a cable.
#
# The headset keeps ADB listening on TCP across reboots (persist.adb.tcp.port),
# so USB is only needed the very first time (enable-quest-wireless.ps1). This
# script re-attaches by trying, in order: an explicit -KnownIp, the last saved
# address, and finally a bounded scan of the PC's own /24 for a device whose
# ADB port answers and that identifies itself as a Quest with root.

$ErrorActionPreference = "Stop"
# A reachable-device probe runs adb against addresses that will not answer, and
# adb prints those failures to stderr. Keep PowerShell from promoting a native
# command's stderr into a terminating error while we test candidates.
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
Push-Location $PSScriptRoot
try {
    $adb = if (-not [string]::IsNullOrWhiteSpace($env:QPRO_ADB) -and (Test-Path -LiteralPath $env:QPRO_ADB)) {
        [System.IO.Path]::GetFullPath($env:QPRO_ADB)
    }
    elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot "platform-tools\adb.exe")) {
        Join-Path $PSScriptRoot "platform-tools\adb.exe"
    }
    else {
        $command = Get-Command adb -ErrorAction SilentlyContinue
        if ($null -ne $command) { $command.Source }
        else { Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe" }
    }
    if (-not (Test-Path -LiteralPath $adb)) {
        throw "adb.exe was not found. Re-extract the release so platform-tools\adb.exe is present."
    }

    $configPath = Join-Path $PSScriptRoot "config\wireless-headset.json"

    function Write-Note([string]$Message) {
        if (-not $Quiet) { Write-Host $Message }
    }

    function Test-Port([string]$Target) {
        # A fast reachability gate so a stale saved address (for example after
        # the headset's DHCP lease moved) fails in milliseconds instead of
        # waiting out adb connect's own multi-second timeout before we scan.
        $hostText, $portText = $Target -split ':', 2
        $portNumber = if ($portText) { [int]$portText } else { $Port }
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $async = $client.BeginConnect($hostText, $portNumber, $null, $null)
            if (-not $async.AsyncWaitHandle.WaitOne(500)) { return $false }
            $client.EndConnect($async)
            return $client.Connected
        }
        catch { return $false }
        finally { $client.Close() }
    }

    function Test-QuestTarget([string]$Target) {
        # A candidate is only accepted when ADB reaches the device state, the
        # product is the Quest Pro, and Magisk still grants root over Wi-Fi.
        # Probing runs adb against addresses that will not answer. This local
        # preference (reverted automatically when the function returns) keeps a
        # native command's stderr from becoming a terminating NativeCommandError
        # under Windows PowerShell 5.1; $LASTEXITCODE drives the decision.
        $ErrorActionPreference = "Continue"
        if (-not (Test-Port $Target)) { return $false }
        $null = & $adb connect $Target 2>$null
        $state = (& $adb -s $Target get-state 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $state -ne "device") {
            $null = & $adb disconnect $Target 2>$null
            return $false
        }
        $device = (& $adb -s $Target shell getprop ro.product.device 2>$null | Out-String).Trim()
        if ($device -ne "seacliff") {
            $null = & $adb disconnect $Target 2>$null
            return $false
        }
        $root = (& $adb -s $Target shell su -c id 2>$null | Out-String)
        if ($root -notmatch 'uid=0\(root\)') {
            Write-Note "Reached $Target but Magisk root was not granted; re-apply root on the headset."
            $null = & $adb disconnect $Target 2>$null
            return $false
        }
        return $true
    }

    function Save-Target([string]$Target) {
        New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "config") | Out-Null
        [ordered]@{
            adbTarget    = $Target
            configuredUtc = [DateTime]::UtcNow.ToString("o")
            transport    = "adb-tcp"
        } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8
    }

    # Candidate addresses to try before scanning, most specific first.
    $candidates = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($KnownIp)) {
        $ip = $KnownIp.Trim()
        $candidates.Add($(if ($ip -match ':') { $ip } else { "${ip}:$Port" }))
    }
    if (Test-Path -LiteralPath $configPath) {
        try {
            $savedTarget = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).adbTarget
            if (-not [string]::IsNullOrWhiteSpace($savedTarget)) { $candidates.Add($savedTarget.Trim()) }
        }
        catch { }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        Write-Note "Trying saved headset address $candidate"
        if (Test-QuestTarget $candidate) {
            Save-Target $candidate
            Write-Output "WIRELESS_ADB_READY $candidate"
            return
        }
    }

    # Fallback: scan the local /24 for an open ADB port, then verify identity.
    $localIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixLength -eq 24 } |
        Sort-Object -Property SkipAsSource | Select-Object -First 1 -ExpandProperty IPAddress
    if ([string]::IsNullOrWhiteSpace($localIp)) {
        throw "The headset was not reachable at its saved address and no /24 network was found to scan. Pass -KnownIp or reconnect USB and run enable-quest-wireless.ps1."
    }
    $prefix = ($localIp -split '\.')[0..2] -join '.'
    Write-Note "Saved address failed; scanning $prefix.0/24 for the headset ADB port $Port..."

    # Probe in batches rather than firing 254 connects at once: a single burst
    # floods ARP resolution for the many dead addresses and the real host's
    # reply can be missed. Each batch waits on its own connect handles.
    $open = [System.Collections.Generic.List[string]]::new()
    $addresses = 1..254 | ForEach-Object { "$prefix.$_" } | Where-Object { $_ -ne $localIp }
    for ($index = 0; $index -lt $addresses.Count; $index += 48) {
        $batch = foreach ($address in $addresses[$index..([Math]::Min($index + 47, $addresses.Count - 1))]) {
            $client = [System.Net.Sockets.TcpClient]::new()
            [pscustomobject]@{
                Address = $address
                Client  = $client
                Async   = $client.BeginConnect($address, $Port, $null, $null)
            }
        }
        # All connects in this batch are already in flight; wait once, then
        # harvest whichever completed without blocking on the dead addresses.
        Start-Sleep -Milliseconds $ScanTimeoutMs
        foreach ($item in $batch) {
            try {
                if ($item.Async.IsCompleted) {
                    $item.Client.EndConnect($item.Async)
                    if ($item.Client.Connected) { $open.Add($item.Address) }
                }
            }
            catch { }
            finally { $item.Client.Close() }
        }
    }
    Write-Note "Hosts with port $Port open: $($open.Count)"

    foreach ($address in $open) {
        $target = "${address}:$Port"
        Write-Note "Verifying $target"
        if (Test-QuestTarget $target) {
            Save-Target $target
            Write-Output "WIRELESS_ADB_READY $target"
            return
        }
    }

    throw "No wireless Quest Pro was found on $prefix.0/24. Confirm the headset is awake on this network, or reconnect USB and run enable-quest-wireless.ps1."
}
finally {
    Pop-Location
}

