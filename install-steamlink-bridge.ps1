param([switch]$Rebuild)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$customLibs = Join-Path $env:APPDATA "VRCFaceTracking\CustomLibs"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$destination = Join-Path $customLibs "000-Qpro.SteamLinkBridge.dll"
$virtualDesktopBridge = Join-Path $customLibs "000-Qpro.IndependentGaze.dll"
# Steam Link VRCFT modules that bind Steam Link's OSC port 9015.
$conflictingModules = @(
    @{ Id = "2a8c8080-2a76-46af-bf76-1da7c0127ef8"; Name = "LinkFT" },
    @{ Id = "b146eda9-be48-4016-ab63-680a694064bd"; Name = "SteamLink VRCFT" }
)

if (Get-Process -Name "VRCFaceTracking" -ErrorAction SilentlyContinue) {
    throw "Close VRCFaceTracking before installing the combined independent-gaze + Steam Link face bridge."
}

$source = Join-Path $root "vrcft-steamlink-bridge\bin\Release\net10.0\Qpro.SteamLinkBridge.dll"
if ($Rebuild -or -not (Test-Path -LiteralPath $source)) {
    $project = Join-Path $root "vrcft-steamlink-bridge\Qpro.SteamLinkBridge.csproj"
    if (-not (Test-Path -LiteralPath $project)) {
        throw "The prebuilt Steam Link VRCFT bridge is missing. Reinstall the release package."
    }
    & dotnet build $project -c Release
    if ($LASTEXITCODE -ne 0) { throw "Building the Steam Link VRCFT bridge failed." }
}
if (-not (Test-Path -LiteralPath $source)) { throw "The bridge DLL was not produced." }

New-Item -ItemType Directory -Force -Path $customLibs | Out-Null
foreach ($module in $conflictingModules) {
    $modulePath = Join-Path $customLibs $module.Id
    if (Test-Path -LiteralPath $modulePath) {
        $backup = Join-Path $root "research\vrcft-steamlink-module-backup-$($module.Id)-$stamp"
        Move-Item -LiteralPath $modulePath -Destination $backup
        Write-Host "Moved the conflicting $($module.Name) module to $backup"
    }
}
# Only one Qpro bridge can own the gaze and tongue ports. The Virtual Desktop
# bridge is a copy of a release file, so Install VD bridge restores it.
if (Test-Path -LiteralPath $virtualDesktopBridge) {
    Remove-Item -LiteralPath $virtualDesktopBridge -Force
    Write-Host "Removed the Virtual Desktop bridge; use Install VD bridge to switch back."
}
Copy-Item -LiteralPath $source -Destination $destination -Force
$sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
$installedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
if ($sourceHash -ne $installedHash) { throw "Installed bridge failed its hash check." }

Write-Host "Installed combined independent-gaze + Steam Link face/tongue bridge: $destination"
Write-Host "In Steam Link set OSC Output Port to 9015 (Custom), then restart VRCFaceTracking."
