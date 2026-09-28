# Quest Pro Tongue + Eye Convergence Tracking

## [Watch the demo!](https://youtu.be/BR_hIHFeo80)
[![Watch the demo](https://github.com/user-attachments/assets/df6e8aab-7081-449b-bb0d-14f7e286a5b3)](https://youtu.be/BR_hIHFeo80)

## ROOT IS REQUIRED FOR THIS TO FUNCITON. IF YOU ARE NOT ON v2.7 OR LOWER THIS WILL NOT WORK
[Root details](https://github.com/Lumince/singularity)

## Download

For normal use, download the complete `QproFaceTracking-<version>.zip` package
from this repository's **Releases** page and extract the whole folder. GitHub's
automatically generated “Source code” archives do not contain the large executable,
pretrained model, private Python installer, or bundled Android tools required to
run the application.

QproFaceTracking is an experimental Quest Pro add-on for VRCFaceTracking. It works
over USB or Wi-Fi. It keeps the normal face, brow, jaw, and blink data from Virtual
Desktop (or Steam Link), then optionally adds:

- independent left/right eyes, so your avatar's eyes can converge and diverge,
  through an eye module on the headset; and
- detailed tongue channels from a personalized stereo model that uses the Quest Pro's
  two lower-face cameras.

This is enthusiast research software, not a polished consumer driver. It requires a
rooted Quest Pro.

## Requirements

- Windows 10 or 11 x64
- Rooted Quest Pro with face and eye tracking enabled
- Magisk Superuser access granted to **Shell / ADB Shell**
- Meta developer mode and an authorized USB debugging connection (also needed once
  to set up Wi-Fi mode)
- No separate ADB installation; the release includes the required official Android
  Platform-Tools files
- SteamVR, Virtual Desktop **or** Steam Link, and VRCFaceTracking
- A current NVIDIA display driver is strongly recommended for fast tongue-model
  training. NVIDIA hardware is optional; CPU training is supported but is much
  slower, especially for the full dataset.
- Live tongue tracking runs through ONNX Runtime + DirectML, so it uses any DirectX 12
  GPU (NVIDIA, AMD or Intel). Training uses PyTorch, which is only GPU-accelerated
  on NVIDIA; on AMD or Intel it trains on the CPU.

## First run
DISCLAIMER: Eye convergence may NOT work on every firmware. It has been tested on
`51483620027600340` (v2.6) and `51503870021300340` (v2.7).

1. Extract the entire release folder. Do not run the executable from inside the zip.
2. Double-click `QproFaceTracking.exe`.
3. Select **Install runtime** (step 1). No preinstalled Python or PATH modification is
   required. The release carries the official signed Python 3.12.10 installer and
   silently installs a private per-user copy plus OpenCV, NumPy, PyTorch, and ONNX
   Runtime under `%LOCALAPPDATA%\QproFaceTracking\runtime`. If Python 3.12 is already
   installed on your PC, setup uses it as the base for its own private environment
   instead of touching your installation. It creates no launcher, shortcuts, file
   associations, or PATH entries. PyTorch is a large download, but later release
   folders reuse the same runtime. Setup uses PyTorch's official CUDA 12.8 wheel when
   an NVIDIA driver/GPU is detected and the official CPU wheel otherwise.
4. Close VRCFaceTracking, then select **Install VD bridge** (Virtual Desktop) or
   **Install Steam Link bridge** (Steam Link, see [Steam Link](#steam-link)) in
   step 2. Restart VRCFT.
5. For eye convergence, connect the rooted headset and open **Manage eye module**
   (step 3). The hub guides you through it; see [Eye convergence](#eye-convergence).
6. Start Virtual Desktop or Steam Link, SteamVR, and VRCFT. Confirm ordinary tracking
   works.
7. Turn on tongue tracking if you want it, choose its settings, then press
   **Apply and start selected**.
8. Press **Stop and restore stock** before disconnecting USB or closing the app.

Pick **USB** or **Wi-Fi** at the top of the hub. Everything works the same over
either connection; see [Wireless](#wireless-adb-over-wi-fi).

Tongue training automatically selects CUDA when PyTorch can access it and falls
back to CPU instead of failing on systems without NVIDIA graphics. The
personalization page shows frame preparation, the active device, checkpoint stage,
epoch count, and overall completion while training is active. The progress panel is
collapsed while idle, and the personalization page has its own scrollbar when the
live status needs more room. CPU mode uses a smaller batch to remain usable
on ordinary PCs, but it can take substantially longer; full-dataset CPU training
may take hours. Installing the CUDA-enabled PyTorch wheel does not replace the
Windows NVIDIA display driver—the driver must already be installed and working.

## Eye convergence

Independent eyes come from a small Magisk module on the headset that patches the
headset's own eye model and turns off the filter that ties the two eyes together. No
Meta files are shipped or downloaded. **Manage eye module** (First-time setup step 3)
always shows where you are and the recommended next step:

1. **Install Sergio's module (recommended).** SergioMarquina's "Quest Pro Individual
   Eye Enabler", bundled unmodified with his permission (`sergio-eye-module/`; the hub
   checks every file's SHA-256 before installing). It supports one specific stock eye
   model, which is used on the firmwares above.
2. **Restart the headset.** After installing, the hub offers to restart the headset for
   you. Please do: on some headsets (for example v2.6) Sergio's installer stops
   controller tracking until the next restart. Re-apply root afterwards if your root
   method needs that.
3. **Automatic check.** After the restart the hub checks that the module really took
   effect: the patched eye model is the one in use and the eye filter is off. The
   status then shows **Convergence on**. You don't need to redo eye-tracking
   calibration.
4. **If it didn't work,** the hub says why (with an error code) and suggests the next
   option: **Create my eye patch**. The hub reads this headset's own eye model, finds
   its eye-blend gate from the model's structure, and builds a small Magisk module
   (`qpro_eye_patch`) that patches your own copy on the headset, verifies it by
   SHA-256 and mounts it at boot. The gate patch is the default; "exact rewire" is an
   experimental alternative. If another module is still patching the model, the hub
   offers to go back to stock first and continues after the restart.

The hub remembers what was tried on your headset, so it doesn't suggest the same thing
twice. If the checks pass but your avatar's eyes still move together, choose **My eyes
still move together** and the hub moves on to the next option.

- **Revert to stock** removes every recognized eye module. Magisk removes it during
  the next restart (the hub offers to restart), and the hub confirms afterwards that
  the stock eye model is back.
- **Check now** re-runs the check at any time. **Restart headset** restarts it from the
  hub (it always asks first).
- Under **Advanced**: **Install module (.zip)** installs another module zip you
  downloaded, after showing its `module.prop` details. **Choose installed** marks a
  module you already installed in the Magisk app. Only install modules you trust, and
  run only one eye-model module at a time.

Install or change eye modules **before** starting Virtual Desktop; changing eye
tracking while VD streams freezes VD's face feed until VD reconnects.

## Wireless (ADB over Wi-Fi)

Pick the connection with the **USB / Wi-Fi** toggle at the top of the hub (default
**USB**). Expect roughly 60–90 Mbit/s upstream for live tongue cameras, so a 5 GHz /
Wi-Fi 6 network (ideally a dedicated VR access point) is recommended alongside Virtual
Desktop. The PC and headset must be on the same network/router.

1. **Pair once over USB.** With the headset connected by USB, select **Wi-Fi** and
   press **Enable / Connect Wi-Fi**. When the headset's IP address appears under
   **Headset link**, you can unplug the cable.
2. **Later sessions:** select **Wi-Fi** and press **Enable / Connect Wi-Fi**. The hub
   reconnects to the saved address (or scans your local network) without a cable.
3. Use tongue capture and **Apply and start selected** exactly as over USB.

ADB is reachable on your local network while wireless is enabled, so only use it on
a trusted private network. `Disable-Wireless.bat` returns the headset to USB-only ADB.

## Steam Link

**Install Steam Link bridge** (step 2) installs a bridge that reads Steam Link's OSC
face and eye data instead of Virtual Desktop's.

- In Steam Link's advanced settings, turn on OSC and eye/face tracking sharing, and
  set the OSC output port to 9015 (Custom).
- Only one bridge can be installed at a time. Each install button removes the other
  bridge. The Steam Link one also moves the LinkFT and SteamLink VRCFT modules into
  `research\`, because they use the same port.
- Steam Link sends one gaze direction for both eyes, so **eye convergence only shows
  up through Virtual Desktop**. Face, blink and tongue tracking work with either.
- The hub switches tongue **Visibility** to **Camera only** while the Steam Link
  bridge is installed, because Steam Link has no native TongueOut signal.

## Included profiles

- `Developer-trained tongue model v8 (demo)` is trained on one person. It is useful
  as an immediate bootstrap/demo and for checking that everything is set up and
  tracking, not a universal model. Quick refinement is recommended for another
  wearer; false positives and blind spots remain possible with different mouths,
  facial hair, clothing, and headset fit.

The hub discovers additional paired tongue files named
`qpro-stereo-tongue-vN-gate.pt` and `qpro-stereo-tongue-vN-direction.pt`. Full or
quick-personalization training chooses the next unused version and never overwrites
the bundled v8 pair.

After a guided capture closes, the hub asks for a friendly dataset name. The raw
timestamped capture remains unchanged for reliability, while the friendly name is
stored in its session metadata and carried into the trained model. Each
personalization card lists completed, untrained datasets; select the one you want
before pressing Train. Successfully trained datasets leave that queue.
When running from a versioned `dist` folder, the hub also discovers captures in
adjacent QproFaceTracking release folders, so an upgrade does not hide recordings
made with the previous build.
On first launch it also copies complete personal tongue model pairs from an
adjacent older release into the new release folder. The public package itself still
contains only the developer v8 demonstration model.

Capture currently requires Virtual Desktop (or Steam Link) tracking, SteamVR, and
VRCFaceTracking to be running because the trainer records Quest Pro's native
`TongueOut` confidence as an auxiliary visibility label. The hub checks these common
prerequisites before opening the guided camera window and explains what is missing
directly.

The **Tongue model manager** tab can rename, export, import, and delete personal
paired models. Export produces one `.qptonguemodel` package containing both neural
network checkpoints. Import assigns the next unused local version. Only import
models from people you trust; PyTorch model files are executable data when loaded.
The bundled developer v8 demo is protected from accidental deletion.

Live tongue tracking converts the selected model once into an ONNX copy and runs it
through ONNX Runtime + DirectML, which uses about 0.3 GB of RAM instead of ~1.1 GB.
If that isn't available, it falls back to PyTorch automatically.

## Tongue controls

- **Motion smoothing:** left/`0` is most responsive; right/`100` is smoothest but
  adds latency. Camera inference is frame-based, so this control filters steps but
  does not create extra tracking samples between camera frames.
- **Weighted camera + native:** recommended default; combines the stereo model with
  Quest Pro's native `TongueOut` confidence.
- **Camera only:** ignores native tongue confidence.
- **Native only:** diagnostic visibility gate; direction still comes from cameras.
- **Conservative agreement:** requires both visibility sources and reduces false
  positives, at the cost of more false negatives.
- **Camera FPS cap:** 24 is the conservative default. Higher choices, up to 72,
  request a faster headset source cadence. On the tested Quest Pro, a 72 cap was
  stable but produced about 36 paired stereo samples per second; it does not imply
  72 completed tongue inferences per second.

## Troubleshooting

When something fails, the hub shows an error code (for example `QPRO-305`), what went
wrong and what to do. The full output is in the **Activity** panel; please include the
code and that text when you ask for help. The most common ones:

| Code | Meaning | What to do |
|---|---|---|
| QPRO-101 / 102 | Python setup problem | Repair or remove an existing Python 3.12, or re-extract the release |
| QPRO-103 | Runtime download failed | Check the internet connection and free disk space, then press Install runtime again |
| QPRO-201 | VRCFaceTracking is open | Close it (also from the tray) before installing a bridge |
| QPRO-302 / 304 | Headset not found / USB debugging not allowed | Wake the headset and accept "Allow USB debugging" |
| QPRO-305 | Root not granted | Magisk > Superuser > enable Shell; re-apply root after a restart |
| QPRO-306 | Wi-Fi unreachable | Same network; connect USB once and press Enable / Connect Wi-Fi |
| QPRO-502 | Sergio's module didn't patch this firmware | Use Create my eye patch |
| QPRO-503 | Your eye patch is inactive | Rebuild it with Create my eye patch |
| QPRO-509 | Headset didn't reconnect after a restart | Put it on so it finishes starting, then press Check now |
| QPRO-602 | Training ran out of memory | Close GPU-heavy apps, or use Quick refinement |

If a window looks cut off at your Windows display scaling, run
`QproFaceTracking.exe --layout-check layout-report` from the release folder. It checks
the layout at 100–200% scaling and saves screenshots plus a report into
`layout-report\`; please attach them to a GitHub issue.

## Building from source and releasing

The repository holds the source code: the WinForms/.NET hub, the VRCFaceTracking
bridges, the Python tracking and training code, and the C headset relay/injector.
A few large files are kept **out of Git** on purpose. These are the bundled
developer tongue model (v8), Android platform-tools, the official Python installer
and the prebuilt headset binaries. `build-release.ps1` downloads them once from the
previous GitHub release into `release-assets\` (ignored by Git), so a fresh clone
can still build a complete release.

### What you need

- Windows 10/11 x64 with the [.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0)
- VRCFaceTracking installed from Steam. The bridges compile against its DLLs. It is
  found automatically; otherwise pass `-VrcftInstallDir "path\to\VRCFaceTracking"`.
- Python 3.12 with NumPy/OpenCV/PyTorch to run the tests. The runtime the hub
  installs works: `%LOCALAPPDATA%\QproFaceTracking\runtime\.venv\Scripts\python.exe`.

### Everyday development

```powershell
# run the tests
python -m unittest discover -p "test_*.py"

# check that the hub compiles, then run it against this checkout
dotnet build qpro-hub\QproFaceTracking.Hub.csproj -c Release
.\qpro-hub\bin\Release\net10.0-windows\QproFaceTracking.Hub.exe --root .

# check the hub's layout at 100-200% Windows scaling
.\qpro-hub\bin\Release\net10.0-windows\QproFaceTracking.Hub.exe --root . --layout-check layout-report
```

When run from source, the hub uses the scripts in the folder given by `--root`. For
anything that talks to the headset, testing a full release build (below) is easier
because it contains adb, the model and the headset binaries.

Developers rebuilding native components can run `.\build-and-run.ps1 -RebuildNative`.

### Making a release

1. Set the new version in `release-manifest.json`, for example `"version": "0.2.1"`.
   The hub title and the zip name use it.
2. Run `.\build-release.ps1`. It compiles everything, bundles the v8 tongue model and
   the other release-only files, runs the packaged hub's self-test and writes
   `dist\QproFaceTracking-<version>.zip`.
3. Try the unzipped `dist\QproFaceTracking-<version>\QproFaceTracking.exe`.
4. On GitHub, open **Releases > Draft a new release**, create the tag `v<version>`,
   write what changed and attach the zip.

To ship a newer model, adb or headset binary, put the file at the same path in your
checkout (for example `models\qpro-stereo-tongue-v8-gate.pt`). Files in the checkout
take priority over the downloaded copies. Don't commit them.

See `CONTRIBUTING.md` and `THIRD_PARTY_NOTICES.md` before redistributing changes.

## Safety and privacy

Inward camera frames are highly sensitive. Live frames remain local and recordings
are created only during explicit calibration. Never publish `captures/`,
`training/`, a generated eye archive, or another person's model without informed
consent. A headset reboot removes injected native code. The launcher also uses a
capture lease and attempts scoped cleanup on every normal exit.

Firmware updates can change provider symbols, trace offsets, or model contracts.
Treat every update as unsupported until revalidated. This project is unaffiliated
with Meta, Virtual Desktop, VRCFaceTracking, VRChat, Project Babble, or EyeTrackVR.
