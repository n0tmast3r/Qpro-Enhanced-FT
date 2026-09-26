# Eye Tracking Model Patcher (Magisk module)

Removes the left/right gaze averaging from the Quest Pro (Seacliff) eye
tracking pipeline at every boot:

1. **Model patch** — the stock model
   `/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/bolt/bolt.ptl`
   is copied to `/data/local/tmp/bolt_patched.ptl` and patched in place
   (dd byte writes at fixed offsets; the .ptl zip is stored uncompressed).
   The patch zeroes the gaze blend gate FC filter (const 42, 4096 bytes) and
   sets its bias (const 43) to -8.0f, so `sigmoid(-8) ~ 3.4e-4` and the model
   outputs the independent per-eye gaze instead of
   `own + g*(other - own)` with g ~= 0.5.
   The patched file is bind-mounted over the stock path; the stock file on
   disk is never modified.
2. **Filter props** — `debug.oculus.eye_tracking.{social,foveation,interaction}_filtering`
   are set to `0`, disabling the downstream per-eye coupling filters in
   `libtrackingengines.so`.
3. **Service restart** — `trackingservice` / `trackingfidelityservice` are
   restarted if they were running (props and model are read at service init).

## Install

- Magisk app → Install from storage → select `model_patcher.zip`, or
- `adb push model_patcher.zip /data/local/tmp/`
  `adb shell su -c 'magisk --install-module /data/local/tmp/model_patcher.zip'`

The patch is applied immediately on install (no reboot needed) and re-applied
at every boot.

## Verify

```
adb shell su -c 'md5sum /odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/bolt/bolt.ptl'
  -> 499f8b1ab40a24e396a6f0c8d1184414   (patched)
adb shell getprop debug.oculus.eye_tracking.social_filtering   -> 0
adb shell su -c 'cat /data/local/tmp/model_patcher.log'
```

## Uninstall / revert

Remove the module in the Magisk app (runs `uninstall.sh`: unmounts the patched
model, restarts services on stock, deletes artifacts) — or simply reboot with
the module disabled. Stock model md5: `e76c2ea88de1e9ff1d7848a2c02ddde8`.

## Safety

- `patch_bolt.sh` verifies the source md5 before patching and the output md5
  after; on any mismatch it aborts and leaves the stock model in place.
- If an OS update changes the model, patching will (safely) fail until offsets
  are re-derived — regenerate with `make_diff_patch.py` on the PC against the
  new stock model, then update `patch_bolt.sh`.
