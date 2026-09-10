param()

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$adb = if (-not [string]::IsNullOrWhiteSpace($env:QPRO_ADB) -and (Test-Path -LiteralPath $env:QPRO_ADB)) {
    [System.IO.Path]::GetFullPath($env:QPRO_ADB)
} elseif (Test-Path -LiteralPath (Join-Path $root "platform-tools\adb.exe")) {
    Join-Path $root "platform-tools\adb.exe"
} else {
    Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
}
$python = if (-not [string]::IsNullOrWhiteSpace($env:QPRO_PYTHON)) { $env:QPRO_PYTHON } else { Join-Path $root ".venv\Scripts\python.exe" }
$fallbackPython = Join-Path $root ".venv\Scripts\qpro-python-console.exe"
if (-not (Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $fallbackPython)) { $python = $fallbackPython }
$patcher = Join-Path $root "research\patch_seacliff_independent_axes.py"
$destination = Join-Path $root "research\seacliff_eye_model\bolt-independent-axes.ptl"
$stockModel = "/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/experimental/bolt/bolt.ptl"
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("qpro-stock-eye-{0}.ptl" -f [Guid]::NewGuid().ToString("N"))

if (-not (Test-Path -LiteralPath $adb)) { throw "Android Platform Tools were not found. Re-extract the release so platform-tools\adb.exe is present." }
if (-not (Test-Path -LiteralPath $python)) { throw "Set up the PC runtime first." }
if (-not (Test-Path -LiteralPath $patcher)) { throw "The local gaze patcher is missing. Reinstall the release package." }

try {
    $state = & $adb get-state 2>&1
    if ($LASTEXITCODE -ne 0 -or ($state -join "`n").Trim() -ne "device") {
        throw "Connect one authorized rooted Quest Pro over USB, then retry."
    }
    $rootProbe = & $adb shell su -c id 2>&1
    if ($LASTEXITCODE -ne 0 -or ($rootProbe -join "`n") -notmatch 'uid=0\(root\)') {
        throw "Grant Magisk Superuser access to Shell / ADB Shell on the headset, then retry."
    }

    Write-Host "Reading the stock eye model from your own headset for local patching..."
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $adb
    $start.Arguments = "exec-out su -c `"cat '$stockModel'`""
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    if (-not $process.Start()) { throw "Could not start ADB." }
    $file = [System.IO.File]::Create($temporary)
    try { $process.StandardOutput.BaseStream.CopyTo($file) } finally { $file.Dispose() }
    $errorText = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Reading the headset eye model failed: $errorText" }
    if ((Get-Item -LiteralPath $temporary).Length -lt 100000) { throw "The headset returned an incomplete eye model." }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    & $python $patcher $temporary $destination
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $destination)) {
        throw "Creating the local independent-eye patch failed."
    }
    Write-Host "Independent-eye support prepared locally: $destination"
    Write-Host "The stock model copy has not been added to the project and will now be deleted."
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
