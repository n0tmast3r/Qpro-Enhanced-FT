param(
    [ValidateRange(1, 120)]
    [int]$MaxFps = 30,
    [ValidateSet("auto", "cpu", "cuda", "cuda:0")]
    [string]$Device = "auto",
    [string]$AdbTarget = "",
    [switch]$Wireless,
    [ValidateRange(0, 999)]
    [int]$Version = 0
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    if ($Wireless -and [string]::IsNullOrWhiteSpace($AdbTarget)) {
        $wirelessConfig = ".\config\wireless-headset.json"
        if (-not (Test-Path -LiteralPath $wirelessConfig)) {
            throw "No saved wireless headset exists. Run enable-quest-wireless.ps1 with USB connected first."
        }
        $AdbTarget = (Get-Content -LiteralPath $wirelessConfig -Raw | ConvertFrom-Json).adbTarget
    }
    $candidates = @()
    foreach ($gate in Get-ChildItem .\models -Filter "qpro-stereo-tongue-v*-gate.pt" -File) {
        if ($gate.Name -match '^qpro-stereo-tongue-v(?<version>\d+)-gate\.pt$') {
            # PowerShell variable names are case-insensitive. Using `$version`
            # here silently overwrote the public `$Version` parameter on every
            # loop iteration, so an explicit `-Version 7` selected v8.
            $candidateVersion = [int]$Matches.version
            $direction = Join-Path $gate.DirectoryName ("qpro-stereo-tongue-v{0}-direction.pt" -f $candidateVersion)
            if (Test-Path -LiteralPath $direction) {
                $candidates += [pscustomobject]@{
                    Version = $candidateVersion
                    Gate = $gate.FullName
                    Direction = (Resolve-Path -LiteralPath $direction).Path
                }
            }
        }
    }
    $latest = if ($Version -gt 0) {
        $candidates | Where-Object Version -eq $Version | Select-Object -First 1
    } else {
        $candidates | Sort-Object Version -Descending | Select-Object -First 1
    }
    if ($null -eq $latest) {
        $requested = if ($Version -gt 0) { "v$Version" } else { "any version" }
        throw "No paired gate/direction tongue checkpoint was found for $requested in .\models."
    }
    Write-Host "Previewing paired tongue model v$($latest.Version)"
    $launcherArguments = @{
        TonguePreview = $true
        MaxFps = $MaxFps
        TongueModelPath = $latest.Gate
        TongueDirectionModelPath = $latest.Direction
        TongueModelDevice = $Device
    }
    if (-not [string]::IsNullOrWhiteSpace($AdbTarget)) {
        $launcherArguments.AdbTarget = $AdbTarget
    }
    & .\build-and-run.ps1 @launcherArguments
    if ($LASTEXITCODE -ne 0) {
        throw "The tongue preview exited with code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
