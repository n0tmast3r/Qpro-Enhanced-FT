# Contributing

Issues and pull requests are welcome. Please keep changes narrowly scoped and state
which Quest firmware, VRCFaceTracking version, connection type, GPU, and model were
tested.

Do not submit raw inward-camera captures, extracted Meta binaries/models, personal
calibration data, or checkpoints trained on people who did not consent to public
redistribution. Synthetic fixtures and summarized diagnostics are preferred.

Before opening a pull request from the full development checkout:

1. Run `python -m unittest discover -p "test_*.py"` from the development repository.
2. Build `qpro-hub/QproFaceTracking.Hub.csproj` in Release mode.
3. Verify that Stop restores the stock eye model, disables tongue output, stops the
   relay, and clears ADB forwarding.
4. Avoid expanding firmware support without a hardware test and explicit fingerprint.

The public proof-of-concept package intentionally omits private captures and the
internal test suite. Contributors cloning that package can still build the managed
projects and run `build-release.ps1`; maintainers should run the full tests before
merging a change.

The combined VRCFT bridge must remain the single owner of final face state. Gaze and
tongue additions must never overwrite stock jaw, lip, cheek, brow, or blink values.
