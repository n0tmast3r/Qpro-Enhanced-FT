param()

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$sharedRoot = Join-Path $env:LOCALAPPDATA "QproFaceTracking\runtime"
$venvRoot = Join-Path $sharedRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$privatePythonRoot = Join-Path $sharedRoot "python-3.12.10"
$privatePython = Join-Path $privatePythonRoot "python.exe"
$bundledPythonInstaller = Join-Path $root "python-runtime\python-3.12.10-amd64.exe"
$bundledPythonSha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
$readyMarker = Join-Path $sharedRoot "runtime-ready.json"
$requirements = Join-Path $root "requirements-runtime.txt"
$torchRequirement = "torch>=2.7,<3"
$cudaIndex = "https://download.pytorch.org/whl/cu128"
$cpuIndex = "https://download.pytorch.org/whl/cpu"

if (-not (Test-Path -LiteralPath $requirements)) {
    throw "requirements-runtime.txt is missing. Reinstall the release package."
}

function Test-PythonCommand([string]$Python, [string]$Code) {
    $previousPreference = $ErrorActionPreference
    try {
        # Import failures are expected while repairing a new/partial environment.
        # Do not let stderr become a terminating NativeCommandError.
        $ErrorActionPreference = "Continue"
        & $Python -c $Code *> $null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Write-ReadyMarker([string]$Python) {
    $payload = @{
        format = "qpro-runtime-ready-v1"
        python = $Python
        completedUtc = [DateTimeOffset]::UtcNow.ToString("O")
    } | ConvertTo-Json
    Set-Content -LiteralPath $readyMarker -Value $payload -Encoding UTF8
}

function Test-UsablePython312([string]$Python) {
    if ([string]::IsNullOrWhiteSpace($Python) -or -not (Test-Path -LiteralPath $Python)) { return $false }
    # Microsoft Store Python redirects writes under %LOCALAPPDATA%, which breaks the
    # private environment. Skip it and let the bundled installer handle that PC.
    if ($Python -like "*\WindowsApps\*") { return $false }
    return Test-PythonCommand $Python "import sys,venv,ensurepip; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)"
}

function Find-ExistingPython312 {
    # A per-user or all-users Python 3.12 that is already installed. The bundled
    # python.org installer cannot install a second private copy next to it: it
    # switches to Modify/Upgrade mode for the existing one instead (issue #1), so
    # setup reuses the existing interpreter only as the *base* of its private
    # virtual environment. The user's installation, PATH and packages are untouched.
    $candidates = [System.Collections.Generic.List[string]]::new()
    foreach ($hive in @("HKCU:", "HKLM:")) {
        $item = Get-ItemProperty -LiteralPath "$hive\Software\Python\PythonCore\3.12\InstallPath" -ErrorAction SilentlyContinue
        if ($null -eq $item) { continue }
        if ($item.PSObject.Properties["ExecutablePath"] -and -not [string]::IsNullOrWhiteSpace($item.ExecutablePath)) {
            $candidates.Add([string]$item.ExecutablePath)
        }
        if ($item.PSObject.Properties["(default)"] -and -not [string]::IsNullOrWhiteSpace($item."(default)")) {
            $candidates.Add((Join-Path ([string]$item."(default)") "python.exe"))
        }
    }
    $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $candidates.Add((Join-Path $env:ProgramFiles "Python312\python.exe"))
    }
    $script:unusablePython312 = $null
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-UsablePython312 $candidate) { return $candidate }
        if ($null -eq $script:unusablePython312 -and -not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate)) {
            $script:unusablePython312 = $candidate
        }
    }
    return $null
}

$nvidiaDetected = $false
$nvidiaName = ""
$nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($null -ne $nvidiaSmi) {
    $nvidiaOutput = @(& $nvidiaSmi.Source --query-gpu=name --format=csv,noheader 2>$null)
    if ($LASTEXITCODE -eq 0 -and $nvidiaOutput.Count -gt 0) {
        $nvidiaDetected = $true
        $nvidiaName = ($nvidiaOutput -join ", ").Trim()
        Write-Host "NVIDIA GPU detected: $nvidiaName"
    }
}

if (-not [string]::IsNullOrWhiteSpace($env:QPRO_PYTHON) -and (Test-Path -LiteralPath $env:QPRO_PYTHON)) {
    if (Test-PythonCommand $env:QPRO_PYTHON "import cv2,numpy,torch; assert hasattr(cv2,'namedWindow')") {
        & $env:QPRO_PYTHON -c "import cv2,numpy,torch; print('Existing runtime ready:', torch.__version__, 'CUDA:', torch.cuda.is_available())"
        Write-Host "An explicitly configured QPRO_PYTHON runtime is ready. Training will automatically use CUDA when that runtime exposes it, otherwise CPU."
        exit 0
    }
}

# A private environment whose base Python was removed or moved no longer starts.
# Rebuild it instead of failing later inside pip.
if ((Test-Path -LiteralPath $venvPython) -and -not (Test-PythonCommand $venvPython "import sys")) {
    Write-Warning "The private Python environment no longer starts (its base Python was probably removed or moved). Rebuilding it."
    Remove-Item -LiteralPath $venvRoot -Recurse -Force
    if (Test-Path -LiteralPath $readyMarker) { Remove-Item -LiteralPath $readyMarker -Force }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    New-Item -ItemType Directory -Force -Path $sharedRoot | Out-Null
    $basePython = $null
    $existingPython = $null
    if (Test-Path -LiteralPath $privatePython) {
        $basePython = $privatePython
        Write-Host "Reusing the private bundled Python installation."
    }
    elseif ($null -ne ($existingPython = Find-ExistingPython312)) {
        $basePython = $existingPython
        Write-Host "Python 3.12 is already installed at $existingPython."
        Write-Host "Using it only as the base for QproFaceTracking's private environment; your Python installation, PATH and packages are not changed."
    }
    elseif ($null -ne $script:unusablePython312 -and $script:unusablePython312 -notlike "*\WindowsApps\*") {
        # Running the bundled installer now would modify or upgrade that existing
        # installation instead of creating a private copy (issue #1). Stop with a fix.
        throw ("Python 3.12 is already installed at $($script:unusablePython312), but it cannot create a private environment (pip/venv is missing). " +
            "Open Windows Settings > Apps, choose Python 3.12 > Modify and make sure pip is included (or uninstall it), then run PC runtime setup again. " +
            "Your Python installation was not changed.")
    }
    elseif (Test-Path -LiteralPath $bundledPythonInstaller) {
        $actualHash = (Get-FileHash -LiteralPath $bundledPythonInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $bundledPythonSha256) {
            throw "The bundled Python installer failed its integrity check. Re-extract or download the release again."
        }
        Write-Host "Installing the bundled Python 3.12 runtime privately. Windows PATH and existing Python installations will not be changed."
        $installLog = Join-Path $sharedRoot "python-install.log"
        $installerArguments = @(
            "/quiet",
            "InstallAllUsers=0",
            "TargetDir=`"$privatePythonRoot`"",
            "Include_launcher=0",
            "InstallLauncherAllUsers=0",
            "Include_pip=1",
            "Include_test=0",
            "Include_doc=0",
            "Include_tcltk=0",
            "Shortcuts=0",
            "AssociateFiles=0",
            "PrependPath=0",
            "AppendPath=0",
            "/log",
            "`"$installLog`""
        )
        $installer = Start-Process -FilePath $bundledPythonInstaller -ArgumentList $installerArguments -WindowStyle Hidden -Wait -PassThru
        if ($installer.ExitCode -in @(0, 3010) -and (Test-Path -LiteralPath $privatePython)) {
            $basePython = $privatePython
        }
        else {
            # The installer reports success but modifies an existing Python 3.12 instead
            # of creating the private copy when one is already registered (issue #1).
            $existingPython = Find-ExistingPython312
            if ($null -ne $existingPython) {
                Write-Host "The bundled installer found an existing Python 3.12 at $existingPython instead of creating a private copy."
                Write-Host "Using it only as the base for QproFaceTracking's private environment."
                $basePython = $existingPython
            }
            else {
                throw ("The bundled Python installation failed with code $($installer.ExitCode). See $installLog`n" +
                    "If Python 3.12 is already installed on this PC, open Windows Settings > Apps, choose Python 3.12 > Modify, " +
                    "make sure pip is included (or uninstall it), then run PC runtime setup again.")
            }
        }
    }
    else {
        # Developer/source checkouts may intentionally omit the redistributable.
        # Keep a system-Python fallback for those checkouts, but public releases
        # include the verified private installer above.
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($null -ne $launcher) {
            $basePython = $launcher.Source
        }
        else {
            $launcher = Get-Command python -ErrorAction SilentlyContinue
            if ($null -ne $launcher) { $basePython = $launcher.Source }
        }
        if ($null -eq $basePython) {
            throw "The bundled Python installer is missing and no developer Python was found. Re-extract the complete release."
        }
    }
    if ($basePython -like "*py.exe") {
        & $basePython -3 -m venv $venvRoot
    }
    else {
        & $basePython -m venv $venvRoot
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw "Creating the local Python environment failed."
    }
}

if (Test-Path -LiteralPath $venvPython) {
    $existingRuntimeReady = Test-PythonCommand $venvPython "import cv2,numpy,torch; assert hasattr(cv2,'namedWindow')"
    $existingCudaReady = $existingRuntimeReady -and (Test-PythonCommand $venvPython "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)")
    if ($existingRuntimeReady -and (-not $nvidiaDetected -or $existingCudaReady)) {
        # Runtimes set up before the low-memory tongue backend lack ONNX Runtime.
        # Add it without touching PyTorch; tongue tracking falls back to PyTorch
        # if this download fails.
        if (-not (Test-PythonCommand $venvPython "import onnx,onnxruntime as o,sys; sys.exit(0 if 'DmlExecutionProvider' in o.get_available_providers() else 1)")) {
            Write-Host "Adding ONNX Runtime (DirectML) for low-memory live tongue tracking."
            $ErrorActionPreference = "Continue"
            # The CPU-only package shares the onnxruntime module name; replace it.
            & $venvPython -m pip uninstall --yes --disable-pip-version-check onnxruntime *> $null
            & $venvPython -m pip install --disable-pip-version-check -r $requirements
            $ErrorActionPreference = "Stop"
            if ($LASTEXITCODE -ne 0) { Write-Warning "ONNX Runtime could not be installed; live tongue tracking will use PyTorch (more memory)." }
        }
        & $venvPython -c "import cv2,numpy,torch; print('Existing shared runtime ready:', torch.__version__, 'CUDA:', torch.cuda.is_available())"
        Write-ReadyMarker $venvPython
        Write-Host "No runtime reinstall was needed."
        exit 0
    }
    if ($existingRuntimeReady -and $nvidiaDetected -and -not $existingCudaReady) {
        Write-Host "The existing runtime is CPU-only even though an NVIDIA GPU is present. Repairing its PyTorch installation."
    }
}

if (Test-Path -LiteralPath $readyMarker) { Remove-Item -LiteralPath $readyMarker -Force }
Write-Host "Installing the shared tracking runtime. PyTorch is large; this may take several minutes."
& $venvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Updating pip failed." }
& $venvPython -m pip install --disable-pip-version-check -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Installing the tracking runtime failed." }

if ($nvidiaDetected) {
    Write-Host "Installing the official CUDA 12.8 PyTorch wheel for $nvidiaName."
    & $venvPython -m pip install --disable-pip-version-check --upgrade $torchRequirement --index-url $cudaIndex
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "The CUDA PyTorch download failed. Installing the CPU build so tracking and training remain usable."
        & $venvPython -m pip install --disable-pip-version-check --upgrade $torchRequirement --index-url $cpuIndex
        if ($LASTEXITCODE -ne 0) { throw "Installing both CUDA and CPU PyTorch builds failed. Check the internet connection and run setup again." }
    }
}
else {
    Write-Host "No NVIDIA driver/GPU was detected. Installing the official CPU PyTorch wheel."
    & $venvPython -m pip install --disable-pip-version-check --upgrade $torchRequirement --index-url $cpuIndex
    if ($LASTEXITCODE -ne 0) { throw "Installing the CPU PyTorch build failed." }
}

if (-not (Test-PythonCommand $venvPython "import cv2,numpy,torch; assert hasattr(cv2,'namedWindow')")) {
    throw "The installed runtime failed its final import check. Run setup again; if it repeats, send the complete Activity log."
}
& $venvPython -c "import cv2,numpy,torch; print('Runtime ready:', torch.__version__, 'CUDA:', torch.cuda.is_available())"

if ($nvidiaDetected) {
    if (-not (Test-PythonCommand $venvPython "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)")) {
        Write-Warning "NVIDIA hardware was detected, but PyTorch still cannot initialize CUDA. Update/reinstall the NVIDIA display driver, then run this setup again. CPU training fallback remains available meanwhile."
    }
}

Write-ReadyMarker $venvPython
Write-Host "PC runtime setup complete. You can close this window and press Refresh in QproFaceTracking."
