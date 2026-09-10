param(
    [switch]$RestoreOnly,
    [switch]$Personalized,
    [switch]$Calibrate,
    [switch]$RuntimePreview,
    [switch]$VrcftOutput,
    [string]$AdbTarget = "",
    [switch]$Wireless,
    [string]$CalibrationOutput = ".\calibration\qpro-independent-visual-axis-v2.json",
    [string]$OverlayPath = ".\third_party\BabbleCalibration-Windows-1.0.8\BabbleCalibration.x86_64.exe",
    [ValidateRange(20, 300)]
    [int]$GazeSeconds = 60,
    [ValidateRange(20, 300)]
    [int]$ConvergenceSeconds = 80,
    [ValidateRange(0, 30)]
    [int]$HeadlessSeconds = 0,
    [string]$StopFile = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$adb = if (-not [string]::IsNullOrWhiteSpace($env:QPRO_ADB) -and (Test-Path -LiteralPath $env:QPRO_ADB)) {
    [System.IO.Path]::GetFullPath($env:QPRO_ADB)
} elseif (Test-Path -LiteralPath (Join-Path $root "platform-tools\adb.exe")) {
    Join-Path $root "platform-tools\adb.exe"
} else {
    Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
}
$hadAndroidSerial = Test-Path Env:ANDROID_SERIAL
$previousAndroidSerial = $env:ANDROID_SERIAL

function Resolve-WorkspacePath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $root $Path))
}

if ($Wireless -and [string]::IsNullOrWhiteSpace($AdbTarget)) {
    $wirelessConfig = Join-Path $root "config\wireless-headset.json"
    if (-not (Test-Path -LiteralPath $wirelessConfig)) {
        throw "No saved wireless headset exists. Run enable-quest-wireless.ps1 with USB connected first."
    }
    $AdbTarget = (Get-Content -LiteralPath $wirelessConfig -Raw | ConvertFrom-Json).adbTarget
}
$python = if (-not [string]::IsNullOrWhiteSpace($env:QPRO_PYTHON)) { $env:QPRO_PYTHON } else { Join-Path $root ".venv\Scripts\python.exe" }
$pythonFallback = Join-Path $root ".venv\Scripts\qpro-python-console.exe"
if (-not (Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $pythonFallback)) {
    $python = $pythonFallback
}
$localModel = Join-Path $root "research\seacliff_eye_model\bolt-independent-axes.ptl"
$remoteModel = "/data/local/tmp/qpro-seacliff-independent-axes.ptl"
$targetModel = "/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/experimental/bolt/bolt.ptl"
$modelProperty = "persist.device_config.oculus_shared_vision.oculus_eyetracking_enable_experimental_model"
$overlay = Resolve-WorkspacePath $OverlayPath
$calibrationOutputPath = Resolve-WorkspacePath $CalibrationOutput

if (-not (Test-Path -LiteralPath $adb)) { throw "ADB not found. Re-extract the release so platform-tools\adb.exe is present." }
if (-not (Test-Path -LiteralPath $python)) { throw "Project Python environment not found under .venv\Scripts." }
if ($Calibrate) {
    if (-not (Test-Path -LiteralPath $overlay)) { throw "BabbleCalibration not found: $overlay" }
    if (-not (Get-Process -Name "vrserver" -ErrorAction SilentlyContinue)) {
        throw "Start SteamVR before visual-axis calibration."
    }
}
if (($RuntimePreview -or $VrcftOutput) -and -not (Test-Path -LiteralPath $calibrationOutputPath)) {
    throw "Independent visual-axis calibration not found: $calibrationOutputPath"
}
if ($VrcftOutput -and -not (Get-Process -Name "VRCFaceTracking" -ErrorAction SilentlyContinue)) {
    throw "Start VRCFaceTracking before enabling gaze-only output."
}

if (-not [string]::IsNullOrWhiteSpace($AdbTarget)) {
    $env:ANDROID_SERIAL = $AdbTarget.Trim()
}

function Invoke-Root([string]$Command, [switch]$AllowFailure) {
    # Windows PowerShell can promote a native process's stderr to a terminating
    # NativeCommandError when the script-wide preference is Stop. Capture the
    # process result first, then apply our own exit-code policy.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $adb shell su -c $Command 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "Headset command failed: $Command`n$output"
    }
    return ($output | Out-String).Trim()
}

function Wait-TrackingService {
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ((Invoke-Root "getprop init.svc.trackingservice" -AllowFailure) -eq "running") {
            Start-Sleep -Milliseconds 1500
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "The headset tracking service did not return to running state."
}

function Restore-StockModel([string]$PropertyValue = "false") {
    Invoke-Root "stop trackingservice" -AllowFailure | Out-Null
    Invoke-Root "setprop $modelProperty $PropertyValue" -AllowFailure | Out-Null
    Invoke-Root "umount '$targetModel'" -AllowFailure | Out-Null
    Invoke-Root "start trackingservice" -AllowFailure | Out-Null
    Wait-TrackingService
    Invoke-Root "rm -f '$remoteModel'" -AllowFailure | Out-Null
}

try {
    & $adb get-state | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No authorized Quest was found over ADB." }
    $rootProbe = & $adb shell su -c id 2>&1
    if ($LASTEXITCODE -ne 0 -or ($rootProbe -join "`n") -notmatch 'uid=0\(root\)') {
        throw "Magisk root is not granted to Android Shell. On the headset open Magisk > Superuser and enable Shell (or ADB Shell), then retry."
    }
    if (-not [string]::IsNullOrWhiteSpace($AdbTarget)) {
        Write-Host "Independent-eye ADB target: $AdbTarget"
    }

    if ($RestoreOnly) {
        Restore-StockModel
        Write-Host "Stock Meta eye model restored."
        exit 0
    }
    $existingMount = Invoke-Root "grep -F '$targetModel' /proc/mounts" -AllowFailure
    if (-not [string]::IsNullOrWhiteSpace($existingMount)) {
        throw "A temporary eye-model test is already active. Close its viewer with Q and wait for 'Stock Meta eye model restored.' If that process is gone, run this script with -RestoreOnly."
    }
    if (-not (Test-Path -LiteralPath $localModel)) {
        throw "Patched research model not found: $localModel"
    }

    $originalProperty = (Invoke-Root "getprop $modelProperty" -AllowFailure)
    if ([string]::IsNullOrWhiteSpace($originalProperty)) { $originalProperty = "false" }
    $cleanupNeeded = $false
    try {
    # Clear a stale temporary override left by an interrupted earlier run.
    Invoke-Root "umount '$targetModel'" -AllowFailure | Out-Null
    & $adb push $localModel $remoteModel | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not copy the research model to the headset." }
    $cleanupNeeded = $true
    Invoke-Root "chown root:root '$remoteModel'" | Out-Null
    Invoke-Root "chmod 0644 '$remoteModel'" | Out-Null
    Invoke-Root "chcon u:object_r:vendor_configs_file:s0 '$remoteModel'" | Out-Null
    Invoke-Root "mount --bind '$remoteModel' '$targetModel'" | Out-Null

    $localHash = (Get-FileHash -LiteralPath $localModel -Algorithm SHA256).Hash.ToLowerInvariant()
    $remoteHash = ((Invoke-Root "sha256sum '$targetModel'") -split "\s+")[0].ToLowerInvariant()
    if ($localHash -ne $remoteHash) { throw "The temporary model failed its headset hash check." }

    Invoke-Root "setprop $modelProperty true" | Out-Null
    Invoke-Root "stop trackingservice" | Out-Null
    Invoke-Root "start trackingservice" | Out-Null
    Wait-TrackingService
    Write-Host "Temporary independent Meta gaze branch active. Q restores the stock model."

    if ($RuntimePreview -or $VrcftOutput) {
        $runtimeArguments = @(
            (Join-Path $root "independent_visual_axis_runtime.py"),
            "--adb", $adb,
            "--calibration", $calibrationOutputPath
        )
        if ($VrcftOutput) { $runtimeArguments += "--output-vrcft" }
        if ($HeadlessSeconds -gt 0) {
            $runtimeArguments += @("--headless-seconds", $HeadlessSeconds)
        }
        if (-not [string]::IsNullOrWhiteSpace($StopFile)) {
            $runtimeArguments += @(
                "--stop-file",
                (Resolve-WorkspacePath $StopFile)
            )
        }
        & $python @runtimeArguments
    }
    elseif ($Calibrate) {
        & $python (Join-Path $root "native_raw_eye_probe.py") `
            --adb $adb `
            --title "Quest Pro independent visual-axis calibration" `
            --notice "TEMPORARY MODEL OVERRIDE - Meta personalization after local branch; Q restores stock" `
            --calibration-overlay $overlay `
            --calibration-output $calibrationOutputPath `
            --gaze-seconds $GazeSeconds `
            --convergence-seconds $ConvergenceSeconds
    }
    elseif ($Personalized) {
        & $python (Join-Path $root "native_raw_eye_probe.py") `
            --adb $adb `
            --title "Quest Pro independent personalized visual axes" `
            --notice "TEMPORARY MODEL OVERRIDE - Meta per-eye calibration after local branch; Q restores stock" `
            --instruction "Test a fixed target around the center and corners, then drift or close one eye; the other ray must remain fixed."
    }
    else {
        & $python (Join-Path $root "native_eye_stage_probe.py") `
            --adb $adb `
            --title "Quest Pro Meta local-branch gaze test" `
            --notice "TEMPORARY MODEL OVERRIDE - stock model is restored when Q quits" `
            --instruction "Close one eye or drift only the right eye; the two bottom axes should now remain independent."
    }
    if ($LASTEXITCODE -ne 0) { throw "The local-branch gaze viewer failed." }
    }
    finally {
        if ($cleanupNeeded) {
            Restore-StockModel $originalProperty
            Write-Host "Stock Meta eye model restored."
        }
    }
}
finally {
    if ($hadAndroidSerial) {
        $env:ANDROID_SERIAL = $previousAndroidSerial
    }
    else {
        Remove-Item Env:ANDROID_SERIAL -ErrorAction SilentlyContinue
    }
}
