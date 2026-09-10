param(
    [string]$AdbTarget = ""
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    if ([string]::IsNullOrWhiteSpace($AdbTarget)) {
        $configPath = ".\config\wireless-headset.json"
        if (-not (Test-Path -LiteralPath $configPath)) {
            throw "Pass -AdbTarget or run enable-quest-wireless.ps1 first."
        }
        $AdbTarget = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).adbTarget
    }
    $adbCommand = Get-Command adb -ErrorAction SilentlyContinue
    $adb = if ($null -ne $adbCommand) {
        $adbCommand.Source
    } else {
        Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
    }
    if (-not (Test-Path -LiteralPath $adb)) { throw "adb.exe was not found." }

    & $adb -s $AdbTarget usb
    $usbExit = $LASTEXITCODE
    $null = & $adb disconnect $AdbTarget 2>&1
    if ($usbExit -ne 0) {
        throw "Could not switch $AdbTarget back to USB mode. Rebooting the headset also disables this ADB TCP session."
    }
    Write-Host "WIRELESS_ADB_DISABLED $AdbTarget"
} finally {
    Pop-Location
}
