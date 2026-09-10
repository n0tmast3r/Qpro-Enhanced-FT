param([switch]$Rebuild)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$customLibs = Join-Path $env:APPDATA "VRCFaceTracking\CustomLibs"
$legacyId = "7f9be083-a4f1-4e30-b28a-8e6ec878d583"
$legacyPath = Join-Path $customLibs $legacyId
$backupPath = Join-Path $root "research\vrcft-legacy-registry-module-backup"
$officialId = "91a90618-b020-4064-8832-809b2ca2b3bc"
$officialPath = Join-Path $customLibs $officialId
$officialBackup = Join-Path $root "research\vrcft-official-virtual-desktop-backup"
$destination = Join-Path $customLibs "000-Qpro.IndependentGaze.dll"

if (Get-Process -Name "VRCFaceTracking" -ErrorAction SilentlyContinue) {
    throw "Close VRCFaceTracking before installing the combined independent-gaze + Virtual Desktop face bridge."
}

$source = Join-Path $root "vrcft-gaze-bridge\bin\Release\net10.0\Qpro.GazeBridge.dll"
if ($Rebuild -or -not (Test-Path -LiteralPath $source)) {
    $project = Join-Path $root "vrcft-gaze-bridge\Qpro.GazeBridge.csproj"
    if (-not (Test-Path -LiteralPath $project)) {
        throw "The prebuilt combined VRCFT bridge is missing. Reinstall the release package."
    }
    & dotnet build $project -c Release
    if ($LASTEXITCODE -ne 0) { throw "Building the VRCFT eye bridge failed." }
}
if (-not (Test-Path -LiteralPath $source)) { throw "The bridge DLL was not produced." }

New-Item -ItemType Directory -Force -Path $customLibs | Out-Null
if (Test-Path -LiteralPath $legacyPath) {
    if (Test-Path -LiteralPath $backupPath) {
        throw "Backup path already exists: $backupPath. Move or remove it before retrying."
    }
    Move-Item -LiteralPath $legacyPath -Destination $backupPath
    Write-Host "Moved the registry-style custom module to $backupPath"
}
if (Test-Path -LiteralPath $officialPath) {
    if (Test-Path -LiteralPath $officialBackup) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $officialBackup = Join-Path $root "research\vrcft-official-virtual-desktop-backup-$stamp"
    }
    Move-Item -LiteralPath $officialPath -Destination $officialBackup
    Write-Host "Moved the conflicting official Virtual Desktop module to $officialBackup"
}
Copy-Item -LiteralPath $source -Destination $destination -Force
$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
$installedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
if ($sourceHash -ne $installedHash) { throw "Installed bridge failed its hash check." }

Write-Host "Installed combined independent-gaze + Virtual Desktop face/tongue bridge: $destination"
Write-Host "Restart VRCFaceTracking normally. Do not reinstall the official Virtual Desktop VRCFT module; this wrapper supplies its stock mappings plus opt-in tongue output."
