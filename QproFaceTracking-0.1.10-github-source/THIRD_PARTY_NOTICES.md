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
  PyTorch, OpenCV, and NumPy are downloaded into that private runtime during setup
  and remain subject to their respective licenses.
- The compiled Qpro VRCFT bridge references VRCFaceTracking assemblies at runtime;
  those assemblies are not copied into this repository.
- No stock Meta model, tracking-service binary, headset configuration, or extracted
  firmware file is included. `prepare-eye-model.ps1` reads a compatible archive from
  the user's own rooted headset, creates a local graph patch, and deletes the
  temporary stock copy. Generated `research/seacliff_eye_model/*.ptl` files must not
  be committed or redistributed.
- The bundled v8 tongue checkpoints contain learned weights from consensually
  recorded developer data, not source camera frames. They are demonstrators and may
  not generalize to another person.
- The three WAV interface sounds in `SFX/` were supplied by the project owner.
  Confirm that you hold redistribution rights for them before publishing a public
  release; replace or omit them if their license is uncertain.

The project source is provided under the MIT License. Third-party names and APIs
remain subject to their respective owners' terms and licenses.
