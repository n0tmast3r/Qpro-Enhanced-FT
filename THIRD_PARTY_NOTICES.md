# Third-party notices and release boundary

QproFaceTracking is independent research and is not affiliated with or endorsed by
Meta, Virtual Desktop, VRCFaceTracking, VRChat, Project Babble, or EyeTrackVR.

- The release bundles the minimal official Android SDK Platform-Tools files needed
  for ADB. Google's accompanying `platform-tools/NOTICE.txt` is included unchanged.
  SteamVR, Virtual Desktop, and VRCFaceTracking remain external software.
- The release bundles the official signed CPython 3.12.10 64-bit Windows installer
  solely to create QproFaceTracking's private per-user runtime. Its unmodified
  license is included at `python-runtime/LICENSE.txt`, and its source URL, SHA-256,
  and packaging-time signature result are recorded in `python-runtime/README.txt`.
  PyTorch, OpenCV, NumPy, ONNX Runtime (DirectML), and ONNX are downloaded into
  that private runtime during setup and remain subject to their respective licenses.
- The compiled Qpro VRCFT bridge references VRCFaceTracking assemblies at runtime;
  those assemblies are not copied into this repository.
- No stock Meta model, tracking-service binary, headset configuration, or extracted
  firmware file is included. Any generated `research/seacliff_eye_model/*.ptl` files
  must not be committed or redistributed. This project ships **no eye-tracking model**.
  Some community eye modules redistribute a modified copy of Meta's proprietary eye model
  (for example inside an OverlayFS image); that content must not be added to this
  repository or any release built from it, and the hub refuses module zips that contain
  a model file or disk image.
- `sergio-eye-module/` is **"Quest Pro Individual Eye Enabler" by SergioMarquina**,
  included unmodified with his permission. It is shell scripts only: it patches the
  headset's own eye model on the device and contains no Meta files. Its files remain
  his work; ask him before reusing them elsewhere.
- The hub's own eye patch (`qpro-hub/EyeModelPatcher.cs`, project MIT license) reads the
  user's own eye model only to compute byte edits and generates a Magisk module that
  applies them to the headset's own copy on the device; the generated module contains
  scripts and a few bytes of patch data, never model content.
- The bundled v8 tongue checkpoints contain learned weights from consensually
  recorded developer data, not source camera frames. They are demonstrators and may
  not generalize to another person.
- The three WAV interface sounds in `SFX/` were supplied by the project owner.
  Confirm that you hold redistribution rights for them before publishing a public
  release; replace or omit them if their license is uncertain.
The project source is provided under the MIT License. Third-party names and APIs
remain subject to their respective owners' terms and licenses.
