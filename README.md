# Quest Pro Tongue + Eye Convergence Tracking

## [Watch the demo!](https://youtu.be/BR_hIHFeo80)
[![Watch the demo](https://github.com/user-attachments/assets/df6e8aab-7081-449b-bb0d-14f7e286a5b3)](https://youtu.be/BR_hIHFeo80)

## ROOT IS REQUIRED FOR THIS TO FUNCITON. IF YOU ARE NOT ON v2.7 OR LOWER THIS WILL NOT WORK
[Root details](https://github.com/Lumince/singularity)

## Download

For normal use, download the complete `QproFaceTracking-<version>-poc.zip` package
from this repository's **Releases** page and extract the whole folder. GitHub's
automatically generated “Source code” archives do not contain the large executable,
pretrained model, private Python installer, or bundled Android tools required to
run the application.

QproFaceTracking is an experimental, USB- or Wi-Fi-connected Quest Pro proof of concept for
VRCFaceTracking. It keeps Virtual Desktop's normal face, brow, jaw, and blink data,
then optionally replaces only:

- left/right gaze with independently preserved detector rays, allowing visible eye
  divergence and convergence; and
- detailed tongue channels with a personalized stereo model using the Quest Pro's
  two lower-face cameras.

This is enthusiast research software, not a polished consumer driver. It requires a
rooted Quest Pro. It runs over USB, or wirelessly over ADB-on-Wi-Fi once the
headset has been paired.

## Requirements

- Windows 10 or 11 x64
- Rooted Quest Pro with face and eye tracking enabled
- Magisk Superuser access granted to **Shell / ADB Shell**
- Meta developer mode and an authorized USB debugging connection
- No separate ADB installation; the release includes the required official Android
  Platform-Tools files
- SteamVR, Virtual Desktop, and VRCFaceTracking
- A current NVIDIA display driver is strongly recommended for fast tongue-model
  training. NVIDIA hardware is optional; CPU training is supported but is much
  slower, especially for the full dataset.
- GPU support for tongue tracking:
  - **Live tracking** runs through ONNX Runtime + DirectML, so it uses any DirectX 12
    GPU: NVIDIA, AMD or Intel. Without DirectML it falls back to PyTorch on the CPU.
  - **Training / personalization** uses PyTorch, which is GPU-accelerated only on
    NVIDIA (CUDA). On AMD or Intel it trains on the CPU, which works but is much slower.
  - Tested so far on an NVIDIA RTX 5080 only; AMD should work through DirectML but has
    not been tested yet.

## First run
EYE CONVERGENCE: independent eyes need a Magisk module that patches the headset's own
eye model and turns off the eye-tracking social filter. The hub installs SergioMarquina's
module (bundled with his permission) and, if that doesn't work on your headset, can build
our own patch from your headset's model.
1. Extract the entire release folder. Do not run the executable from inside the zip.
2. Double-click `QproFaceTracking.exe`.
3. Select **Set up PC runtime**. No preinstalled Python or PATH modification is
   required. The release carries the official signed Python 3.12.10 installer and
   silently installs a private per-user copy plus OpenCV, NumPy, PyTorch, and
   ONNX Runtime (DirectML) under
4. Close VRCFaceTracking, then select **Install/update bridge**. Restart VRCFT.
5. Pick the **Connection** at the top of the hub — **USB** or **Wi-Fi**.
   Eye convergence comes from an **independent-eye Magisk module** on the headset.
   First-time setup **step 3 → Manage eye module** offers, in this order:
   - **Install Sergio's module (recommended)** — SergioMarquina's "Quest Pro Individual
     Eye Enabler", bundled unmodified with his permission (`sergio-eye-module/`; the hub
     checks every file's SHA-256 before installing). It patches the headset's own eye
     model on-device. It supports one specific stock eye model, so on other firmware it
     refuses to install.
   - **Create my eye patch** — the fallback if Sergio's doesn't install or doesn't give
     independent eyes. The hub reads this headset's own eye model, finds its eye-blend
     gate from the model's structure, and builds a small Magisk module (`qpro_eye_patch`)
     that patches your own copy on the headset, verifies it by SHA-256, mounts it at
     boot and turns off the eye-tracking social filter. The gate patch is the default;
     "exact rewire" is an experimental option not yet tested on a headset.
6. Start Virtual Desktop(or restart it), SteamVR, and VRCFT. Confirm ordinary tracking works.
7. Turn on tongue tracking if you want it, choose its settings, then press
   **Apply and start selected**. Eye gaze runs from the eye module on the headset; Apply
   checks it is active unless **Skip eye gaze (tongue only)** is on.
8. Press **Stop and restore stock** before disconnecting USB or closing the app.

## Wireless (ADB over Wi-Fi)

Pick the connection explicitly with the **USB / Wi-Fi** toggle at the top of the
hub (default **USB**).expect roughly 60–90 Mbit/s upstream for live tongue cameras, 
so a 5 GHz / Wi-Fi 6 network
(ideally a dedicated VR access point) is recommended alongside Virtual Desktop. The
PC and headset must be on the same network/router.

1. **Pair once over USB.** With the headset connected by USB, press Wi-Fi at the top, and
   press Enable/Connect Wifi. Then, when you see your headset's IP address under the
   "Headset Link" text, then your good to unplug your headset.
        
4. Use tongue capture and **Apply and start selected** exactly as over USB. The hub
   forwards the wireless target to the tracking and capture scripts automatically.

> The **USB / Wi-Fi** toggle is transport only — eye convergence comes from an
> independent-eye Magisk module (Sergio's, our own eye patch, or one you supply); the
> hub installs and detects it over either transport. No eye-tracking model is included
> in this project. The original PC gaze runtime is no longer used by the hub. Tongue
> tracking has no firmware dependency and works on either transport.

Tongue training automatically selects CUDA when PyTorch can access it and falls
back to CPU instead of failing on systems without NVIDIA graphics. The
personalization page shows frame preparation, the active device, checkpoint stage,
epoch count, and overall completion while training is active. The progress panel is
collapsed while idle, and the personalization page has its own scrollbar when the
live status needs more room. CPU mode uses a smaller batch to remain usable
on ordinary PCs, but it can take substantially longer; full-dataset CPU training
may take hours. Installing the CUDA-enabled PyTorch wheel does not replace the
Windows NVIDIA display driver—the driver must already be installed and working.



## Included profiles

- `Developer-trained tongue model v8 (demo)` is trained on one person. It is useful
  as an immediate bootstrap/demo, not a universal model. Quick refinement is
  recommended for another wearer; false positives and blind spots remain possible
  with different mouths, facial hair, clothing, and headset fit.

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

Capture currently requires Virtual Desktop tracking, SteamVR, and VRCFaceTracking
to be running because the trainer records Quest Pro's native `TongueOut` confidence
as an auxiliary visibility label. The hub checks these common prerequisites before
opening the guided camera window and explains what is missing directly.

The **Tongue model manager** tab can rename, export, import, and delete personal
paired models. Export produces one `.qptonguemodel` package containing both neural
network checkpoints. Import assigns the next unused local version. Only import
models from people you trust; PyTorch model files are executable data when loaded.
The bundled developer v8 demo is protected from accidental deletion.

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

## Source and development

The main repository contains the editable WinForms/.NET hub, Python tracking and
training code, and C headset relay/injector sources. Large generated executables,
the developer-trained model, Android tools, and the private Python installer are
kept out of Git history and distributed in the versioned Release package instead.
That keeps clones reviewable while ordinary users still receive a complete build.

A source-only clone can build and edit the managed/Python/C components, but the
full `build-release.ps1` packaging step also requires maintainer-staged assets that
are intentionally not kept in Git: the developer model checkpoints, official
Python installer, Android Platform Tools, and prebuilt rooted-headset binaries.
GitHub does not automatically produce a runnable package from the repository;
official runnable builds are attached explicitly on the Releases page.

Developers rebuilding native components can run:

```powershell
.\build-and-run.ps1 -RebuildNative
```

Developers rebuilding managed helpers can use `-RebuildManaged` or build the
individual `.csproj` files. See `CONTRIBUTING.md` and `THIRD_PARTY_NOTICES.md` before
redistributing changes.

## Safety and privacy

Inward camera frames are highly sensitive. Live frames remain local and recordings
are created only during explicit calibration. Never publish `captures/`,
`training/`, a generated eye archive, or another person's model without informed
consent. A headset reboot removes injected native code. The launcher also uses a
capture lease and attempts scoped cleanup on every normal exit.

Firmware updates can change provider symbols, trace offsets, or model contracts.
Treat every update as unsupported until revalidated. This project is unaffiliated
with Meta, Virtual Desktop, VRCFaceTracking, VRChat, Project Babble, or EyeTrackVR.
