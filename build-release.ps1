<#
Builds the complete, ready-to-upload release zip in dist\.

  .\build-release.ps1

The version comes from release-manifest.json (change it there). Large files that
are deliberately not kept in Git (the bundled tongue model, Android platform-tools,
the Python installer and the prebuilt headset binaries) are taken from this checkout
if present, otherwise downloaded once from a previous GitHub release into
release-assets\ (git-ignored). See README "Building from source and releasing".
#>
param(
    [string]$Version,
    # Where missing release-only assets come from: a release .zip URL, a local .zip,
    # or an extracted release folder.
    [string]$AssetSource = "https://github.com/n0tmast3r/Qpro-Enhanced-FT/releases/download/v0.1.10/QproFaceTracking-0.1.10-poc.zip",
    # VRCFaceTracking install folder (its DLLs are needed to build the VRCFT bridges).
    # Found automatically in Steam libraries when omitted.
    [string]$VrcftInstallDir
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$manifestPath = Join-Path $root "release-manifest.json"
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json).version
}
if ([string]::IsNullOrWhiteSpace($Version)) { throw "No version given and release-manifest.json has none." }
# Numeric part for the .exe file properties (0.2.0-beta -> 0.2.0).
$fileVersion = ($Version -split '[-+]')[0]
Write-Host "Building QproFaceTracking $Version"

# ---- Release-only assets (not in Git) -------------------------------------------
$pythonInstallerSha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
$releaseAssets = @(
    "libquestpro-camera-streamer-v8.so",
    "questpro-camera-relay-v8",
    "questpro-camera-injector",
    "models\qpro-stereo-tongue-v8-gate.pt",
    "models\qpro-stereo-tongue-v8-direction.pt",
    "platform-tools\adb.exe",
    "platform-tools\AdbWinApi.dll",
    "platform-tools\AdbWinUsbApi.dll",
    "platform-tools\NOTICE.txt",
    "platform-tools\source.properties",
    "python-runtime\python-3.12.10-amd64.exe",
    "python-runtime\LICENSE.txt",
    "python-runtime\README.txt"
)
$assetCache = Join-Path $root "release-assets"

function Resolve-SourceFile([string]$RelativePath) {
    foreach ($base in @($root, $assetCache)) {
        $candidate = Join-Path $base $RelativePath
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

$missingAssets = @($releaseAssets | Where-Object { $null -eq (Resolve-SourceFile $_) })
if ($missingAssets.Count -gt 0) {
    Write-Host "Fetching $($missingAssets.Count) release-only file(s) (tongue model, adb, Python installer, headset binaries) from: $AssetSource"
    $extractRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("qpro-release-assets-" + [guid]::NewGuid().ToString("N"))
    try {
        if (Test-Path -LiteralPath $AssetSource -PathType Container) {
            $baseRelease = (Resolve-Path -LiteralPath $AssetSource).Path
        }
        else {
            $zip = $AssetSource
            if ($AssetSource -match '^https?://') {
                New-Item -ItemType Directory -Force -Path $assetCache | Out-Null
                $zip = Join-Path $assetCache "base-release.zip"
                if (-not (Test-Path -LiteralPath $zip)) {
                    $previousProgress = $ProgressPreference
                    $ProgressPreference = "SilentlyContinue"   # much faster downloads in Windows PowerShell
                    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
                    try { Invoke-WebRequest -Uri $AssetSource -OutFile "$zip.partial" -UseBasicParsing }
                    finally { $ProgressPreference = $previousProgress }
                    Move-Item -LiteralPath "$zip.partial" -Destination $zip -Force
                }
            }
            if (-not (Test-Path -LiteralPath $zip)) { throw "Release asset source not found: $AssetSource" }
            Expand-Archive -LiteralPath $zip -DestinationPath $extractRoot -Force
            $baseRelease = $extractRoot
            $nested = @(Get-ChildItem -LiteralPath $extractRoot -Directory)
            if (-not (Test-Path -LiteralPath (Join-Path $extractRoot "release-manifest.json")) -and $nested.Count -eq 1) {
                $baseRelease = $nested[0].FullName
            }
        }
        $sums = @{}
        $sumsFile = Join-Path $baseRelease "SHA256SUMS.txt"
        if (Test-Path -LiteralPath $sumsFile) {
            foreach ($line in Get-Content -LiteralPath $sumsFile) {
                if ($line -match '^([0-9a-f]{64})\s+(.+)$') { $sums[$Matches[2].Trim().Replace('/', '\')] = $Matches[1] }
            }
        }
        foreach ($asset in $missingAssets) {
            $from = Join-Path $baseRelease $asset
            if (-not (Test-Path -LiteralPath $from)) { throw "The asset source does not contain $asset." }
            if ($sums.ContainsKey($asset)) {
                $hash = (Get-FileHash -LiteralPath $from -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($hash -ne $sums[$asset]) { throw "$asset does not match the source release's SHA256SUMS.txt." }
            }
            $to = Join-Path $assetCache $asset
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
            Copy-Item -LiteralPath $from -Destination $to -Force
        }
    }
    finally {
        if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
    }
}
$pythonInstaller = Resolve-SourceFile "python-runtime\python-3.12.10-amd64.exe"
if ((Get-FileHash -LiteralPath $pythonInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pythonInstallerSha256) {
    throw "python-runtime\python-3.12.10-amd64.exe failed its integrity check. Delete release-assets\ and run again."
}

# ---- VRCFaceTracking reference DLLs for the bridges ------------------------------
if ([string]::IsNullOrWhiteSpace($VrcftInstallDir)) {
    $libraries = [System.Collections.Generic.List[string]]::new()
    $steam = (Get-ItemProperty -LiteralPath "HKCU:\Software\Valve\Steam" -ErrorAction SilentlyContinue).SteamPath
    if ($steam) {
        $libraries.Add($steam)
        $vdf = Join-Path $steam "steamapps\libraryfolders.vdf"
        if (Test-Path -LiteralPath $vdf) {
            foreach ($match in [regex]::Matches((Get-Content -Raw -LiteralPath $vdf), '"path"\s+"([^"]+)"')) {
                $libraries.Add($match.Groups[1].Value.Replace('\\', '\'))
            }
        }
    }
    $libraries.Add("H:\SteamLibrary")
    foreach ($library in ($libraries | Select-Object -Unique)) {
        $candidate = Join-Path $library "steamapps\common\VRCFaceTracking"
        if (Test-Path -LiteralPath (Join-Path $candidate "VRCFaceTracking.Core.dll")) { $VrcftInstallDir = $candidate; break }
    }
}
if ([string]::IsNullOrWhiteSpace($VrcftInstallDir) -or -not (Test-Path -LiteralPath (Join-Path $VrcftInstallDir "VRCFaceTracking.Core.dll"))) {
    throw "VRCFaceTracking was not found. Install it from Steam, or pass -VrcftInstallDir 'path\to\VRCFaceTracking'."
}
Write-Host "Using VRCFaceTracking from $VrcftInstallDir"
$versionArguments = @("-p:Version=$Version", "-p:FileVersion=$fileVersion.0", "-p:AssemblyVersion=$fileVersion.0", "-p:IncludeSourceRevisionInInformationalVersion=false")
$safeVersion = $Version -replace '[^A-Za-z0-9._-]', '-'
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $root "dist"))
$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $distRoot "QproFaceTracking-$safeVersion"))
$artifactRoot = [System.IO.Path]::GetFullPath((Join-Path $root "artifacts\release-$safeVersion"))

if (-not $releaseRoot.StartsWith($distRoot + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe release target: $releaseRoot"
}
if (Test-Path -LiteralPath $releaseRoot) {
    # A previous build of this version may still be in use: the hub started from it, or
    # the adb server that hub launched (adb keeps running in the background after the hub
    # closes). Windows won't delete a running .exe, so deal with those first.
    $prefix = $releaseRoot.TrimEnd('\') + '\'
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $path = $null
        try { $path = $_.Path } catch { }
        $path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($running | Where-Object { $_.ProcessName -like "QproFaceTracking*" }) {
        throw "QproFaceTracking is still open from the previous build ($releaseRoot). Close it and run this again."
    }
    foreach ($process in $running) {
        Write-Host "Stopping $($process.ProcessName) from the previous build so its folder can be replaced..."
        if ($process.ProcessName -eq "adb") { try { & $process.Path kill-server *> $null } catch { } }
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    if ($running.Count -gt 0) { Start-Sleep -Seconds 1 }
    Remove-Item -LiteralPath $releaseRoot -Recurse -Force
}
if (Test-Path -LiteralPath $artifactRoot) { Remove-Item -LiteralPath $artifactRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $releaseRoot, $artifactRoot | Out-Null

Write-Host "Publishing the self-contained Windows hub..."
$hubPublish = Join-Path $artifactRoot "hub"
& dotnet publish (Join-Path $root "qpro-hub\QproFaceTracking.Hub.csproj") -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:DebugType=None @versionArguments -o $hubPublish
if ($LASTEXITCODE -ne 0) { throw "Publishing the Windows hub failed." }
Copy-Item -LiteralPath (Join-Path $hubPublish "QproFaceTracking.Hub.exe") -Destination (Join-Path $releaseRoot "QproFaceTracking.exe")

Write-Host "Publishing the self-contained label bridge..."
$labelPublish = Join-Path $artifactRoot "label-bridge"
& dotnet publish (Join-Path $root "vd-label-bridge\Qpro.VirtualDesktopLabelBridge.csproj") -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:DebugType=None @versionArguments -o $labelPublish
if ($LASTEXITCODE -ne 0) { throw "Publishing the label bridge failed." }
$labelDestination = Join-Path $releaseRoot "vd-label-bridge\bin\Release\net10.0"
New-Item -ItemType Directory -Force -Path $labelDestination | Out-Null
Copy-Item -LiteralPath (Join-Path $labelPublish "Qpro.VirtualDesktopLabelBridge.exe") -Destination $labelDestination

Write-Host "Building the combined VRCFT bridge..."
& dotnet build (Join-Path $root "vrcft-gaze-bridge\Qpro.GazeBridge.csproj") -c Release "-p:VrcftInstallDir=$VrcftInstallDir"
if ($LASTEXITCODE -ne 0) { throw "Building the combined VRCFT bridge failed." }
$vrcftBinaryDestination = Join-Path $releaseRoot "vrcft-gaze-bridge\bin\Release\net10.0"
New-Item -ItemType Directory -Force -Path $vrcftBinaryDestination | Out-Null
Copy-Item -LiteralPath (Join-Path $root "vrcft-gaze-bridge\bin\Release\net10.0\Qpro.GazeBridge.dll") -Destination $vrcftBinaryDestination

Write-Host "Building the Steam Link VRCFT bridge..."
& dotnet build (Join-Path $root "vrcft-steamlink-bridge\Qpro.SteamLinkBridge.csproj") -c Release "-p:VrcftInstallDir=$VrcftInstallDir"
if ($LASTEXITCODE -ne 0) { throw "Building the Steam Link VRCFT bridge failed." }
$steamLinkBinaryDestination = Join-Path $releaseRoot "vrcft-steamlink-bridge\bin\Release\net10.0"
New-Item -ItemType Directory -Force -Path $steamLinkBinaryDestination | Out-Null
Copy-Item -LiteralPath (Join-Path $root "vrcft-steamlink-bridge\bin\Release\net10.0\Qpro.SteamLinkBridge.dll") -Destination $steamLinkBinaryDestination

function Copy-ReleaseFile([string]$RelativePath) {
    $source = Resolve-SourceFile $RelativePath
    if ($null -eq $source) { throw "Required release file is missing: $RelativePath" }
    $destination = Join-Path $releaseRoot $RelativePath
    $parent = Split-Path -Parent $destination
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

$runtimeFiles = @(
    "build-and-run.ps1",
    "preview-latest-tongue.ps1",
    "install-vrcft-eye-bridge.ps1",
    "enable-quest-wireless.ps1",
    "disable-quest-wireless.ps1",
    "connect-quest-wireless.ps1",
    "Enable-Wireless.bat",
    "Disable-Wireless.bat",
    "install-steamlink-bridge.ps1",
    "setup-runtime.ps1",
    "train-latest-tongue-stills.ps1",
    "train-latest-tongue-refinement.ps1",
    "requirements-runtime.txt",
    "receiver.py",
    "capture_format.py",
    "calibration.py",
    "tongue_calibration.py",
    "tongue_still_capture.py",
    "label_capture.py",
    "tongue_model_preview.py",
    "export_tongue_onnx.py",
    "train_tongue_model.py",
    "prepare_tongue_stills.py",
    "prepare_tongue_training.py",
    "prepare_training.py",
    "calibration_inspect.py",
    "dataset_inspect.py",
    "eye_signal_filter.py",
    "native_eye_probe.py",
    "native_eye_stage_probe.py",
    "native_raw_eye_probe.py",
    "native_eye_pupil_probe.py",
    "open_source_preview.py",
    "visual_axis_calibration.py",
    "stereo_eye_calibration.py",
    "libquestpro-camera-streamer-v8.so",
    "questpro-camera-relay-v8",
    "questpro-camera-injector",
    "calibration\qpro-independent-visual-axis-v2.json",
    "models\qpro-stereo-tongue-v8-gate.pt",
    "models\qpro-stereo-tongue-v8-direction.pt",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "release-manifest.json"
)
foreach ($file in $runtimeFiles) { Copy-ReleaseFile $file }
foreach ($file in @("succeed.wav", "trainingComplete.wav", "warning.wav")) {
    Copy-ReleaseFile ("SFX\" + $file)
}
# SergioMarquina's eye module, shipped unmodified with his permission (see THIRD_PARTY_NOTICES.md).
foreach ($file in @("module.prop", "customize.sh", "patch_bolt.sh", "service.sh", "uninstall.sh", "README.md")) {
    Copy-ReleaseFile ("sergio-eye-module\" + $file)
}
foreach ($file in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "source.properties")) {
    Copy-ReleaseFile ("platform-tools\" + $file)
}
foreach ($file in @("python-3.12.10-amd64.exe", "LICENSE.txt", "README.txt")) {
    Copy-ReleaseFile ("python-runtime\" + $file)
}
# A maintainer-only RELEASE_README.md wins if present; otherwise ship the repo README.
$releaseReadme = Resolve-SourceFile "RELEASE_README.md"
if ($null -eq $releaseReadme) { $releaseReadme = Join-Path $root "README.md" }
Copy-Item -LiteralPath $releaseReadme -Destination (Join-Path $releaseRoot "README.md")

New-Item -ItemType Directory -Force -Path (Join-Path $releaseRoot "captures"), (Join-Path $releaseRoot "training"), (Join-Path $releaseRoot "research\seacliff_eye_model") | Out-Null
Set-Content -LiteralPath (Join-Path $releaseRoot "captures\.gitkeep") -Value ""
Set-Content -LiteralPath (Join-Path $releaseRoot "training\.gitkeep") -Value ""
Set-Content -LiteralPath (Join-Path $releaseRoot "research\seacliff_eye_model\.gitkeep") -Value ""

$forbidden = @(
    Get-ChildItem -LiteralPath $releaseRoot -Recurse -File | Where-Object {
        $_.Extension -in @(".qpcap", ".qplabel", ".jsonl") -or
        $_.Name -eq "bolt-independent-axes.ptl" -or
        $_.Name -eq "GITHUB_PUBLISHING.md" -or
        $_.Name -like "*.csproj" -or
        $_.Name -in @("Program.cs", "EyeModelPatcher.cs", "TrackingModule.cs", "streamer.c", "relay.c", "injector.c", "build-release.ps1", "build-github-source.ps1") -or
        $_.FullName -match '\\(test_|__pycache__|training\\.+\.(npy|npz))'
    }
)
if ($forbidden.Count) { throw "Private/test artifacts entered the release: $($forbidden.FullName -join ', ')" }

$hashLines = Get-ChildItem -LiteralPath $releaseRoot -Recurse -File |
    Where-Object Name -ne "SHA256SUMS.txt" |
    Sort-Object FullName |
    ForEach-Object {
        $relative = $_.FullName.Substring($releaseRoot.Length + 1).Replace('\', '/')
        "{0}  {1}" -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $relative
    }
Set-Content -LiteralPath (Join-Path $releaseRoot "SHA256SUMS.txt") -Value $hashLines -Encoding utf8

$archive = "$releaseRoot.zip"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
Compress-Archive -LiteralPath $releaseRoot -DestinationPath $archive -CompressionLevel Optimal

Write-Host "RELEASE_READY folder=$releaseRoot"
Write-Host "RELEASE_READY zip=$archive"

# Final check: the packaged hub verifies that every required file is present.
$selfTest = Join-Path $artifactRoot "self-test.json"
& (Join-Path $releaseRoot "QproFaceTracking.exe") --self-test $selfTest | Out-Null
$check = Get-Content -Raw -LiteralPath $selfTest | ConvertFrom-Json
if (-not $check.ok) { throw "The packaged release failed its self-test. Missing: $($check.missingFiles -join ', ')" }
Write-Host "Release self-test passed (tongue models: $($check.tonguePairs -join ', '))."
Write-Host ""
Write-Host "To publish: on GitHub open Releases > Draft a new release, create tag v$Version,"
Write-Host "and attach $(Split-Path -Leaf $archive)."
