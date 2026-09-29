param(
    [string]$UsbSerial = "",
    [ValidateRange(1024, 65535)]
    [int]$Port = 5555
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    # Prefer the bundled Platform-Tools so a friend without adb on PATH or an
    # Android SDK still works out of the box (QPRO_ADB override first).
    $adb = if (-not [string]::IsNullOrWhiteSpace($env:QPRO_ADB) -and (Test-Path -LiteralPath $env:QPRO_ADB)) {
        [System.IO.Path]::GetFullPath($env:QPRO_ADB)
    }
    elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot "platform-tools\adb.exe")) {
        Join-Path $PSScriptRoot "platform-tools\adb.exe"
    }
    else {
        $adbCommand = Get-Command adb -ErrorAction SilentlyContinue
        if ($null -ne $adbCommand) { $adbCommand.Source }
        else { Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe" }
    }
    if (-not (Test-Path -LiteralPath $adb)) {
        throw "adb.exe was not found. Re-extract the whole release so platform-tools\adb.exe is present next to this script."
    }

    if ([string]::IsNullOrWhiteSpace($UsbSerial)) {
        # Wrap the entire pipeline, not only adb's output. PowerShell otherwise
        # unwraps one result into a scalar string and $devices[0] becomes the
        # first character of the serial (for example "2").
        $devices = @(
            @(& $adb devices) | ForEach-Object {
                if ($_ -match '^([^\s:]+)\s+device$') { $Matches[1] }
            }
        )
        if ($devices.Count -ne 1) {
            throw "Connect exactly one Quest by USB, or pass -UsbSerial. Found $($devices.Count) USB devices."
        }
        $UsbSerial = [string]$devices[0]
    }

    # Both the relay and the provider injector require Magisk root. Prompt for
    # and verify that permission while USB is still connected, when recovering
    # from a denied Shell request is least confusing.
    $rootProbe = (& $adb -s $UsbSerial shell su -c id 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $rootProbe -notmatch 'uid=0\(root\)') {
        throw @"
Magisk has not granted superuser access to Android Shell.
On the headset, open Magisk > Superuser and enable the entry named Shell (or ADB Shell).
If no entry is visible, rerun this command while watching the headset and approve the prompt.
No wireless setting was changed. Do not install questcam-magisk.zip for this project.
"@
    }

    # The headset's real Wi-Fi address is the wlan0 interface address. Deriving it
    # from the default route ("ip route get 1.1.1.1") can return a different
    # interface's source address, which is then not reachable over Wi-Fi. Use
    # wlan0, and fall back to the route source only if wlan0 has no address.
    $wlan = (& $adb -s $UsbSerial shell ip -4 addr show wlan0 2>&1) -join " "
    $wifiIp = $null
    if ($wlan -match '\binet\s+(?<ip>\d+\.\d+\.\d+\.\d+)') { $wifiIp = $Matches.ip }
    if (-not $wifiIp) {
        $route = (& $adb -s $UsbSerial shell ip -4 route get 1.1.1.1 2>&1) -join " "
        if ($route -match '\bsrc\s+(?<ip>\d+\.\d+\.\d+\.\d+)') { $wifiIp = $Matches.ip }
    }

    Write-Host "Switching the USB-authorized headset ADB daemon to TCP port $Port"
    & $adb -s $UsbSerial tcpip $Port
    if ($LASTEXITCODE -ne 0) { throw "Enabling ADB-over-Wi-Fi failed." }
    Start-Sleep -Seconds 2

    # Delegate the connection to connect-quest-wireless.ps1: it tries the detected
    # address and, if that is wrong (a different interface, or a moved DHCP lease),
    # scans the local network for the headset, verifies root, and saves the target.
    $connectScript = Join-Path $PSScriptRoot "connect-quest-wireless.ps1"
    if (-not (Test-Path -LiteralPath $connectScript)) {
        throw "connect-quest-wireless.ps1 is missing next to this script; re-extract the release."
    }
    $env:QPRO_ADB = $adb
    $connectArgs = @{ Port = $Port }
    if ($wifiIp) { $connectArgs['KnownIp'] = $wifiIp }
    $connectOutput = & $connectScript @connectArgs
    $connectOutput | Where-Object { $_ -notmatch '^WIRELESS_ADB_READY ' } | ForEach-Object { Write-Host $_ }
    $ready = $connectOutput | Where-Object { $_ -match '^WIRELESS_ADB_READY (.+)$' } | Select-Object -Last 1
    if (-not $ready) {
        throw "ADB over Wi-Fi was enabled, but the headset could not be reached on the network. Make sure the PC and headset are on the same Wi-Fi/router, then unplug USB and press Enable / Connect Wi-Fi again."
    }

    Write-Output $ready
    Write-Warning "ADB is reachable on the local network until headset reboot or disable-quest-wireless.ps1. Use only a trusted private network."
} finally {
    Pop-Location
}
