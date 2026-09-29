MODDIR=${0%/*}
MODEL=/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/bolt/bolt.ptl
OUT=/data/local/tmp/bolt_patched.ptl
LOG=/data/local/tmp/model_patcher.log

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; echo "[model_patcher] $*"; }

setprop debug.oculus.eye_tracking.social_filtering 0
setprop debug.oculus.eye_tracking.foveation_filtering 0
setprop debug.oculus.eye_tracking.interaction_filtering 0

log "props set: social/foveation/interaction filtering = 0"

i=0
while [ ! -f "$MODEL" ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
if [ ! -f "$MODEL" ]; then
  log "[-] model file never appeared: $MODEL"
  exit 1
fi

if ! sh "$MODDIR/patch_bolt.sh" >> "$LOG" 2>&1; then
  log "patch failed on first try, umounting $MODEL and retrying"
  umount "$MODEL" 2>/dev/null
  if ! sh "$MODDIR/patch_bolt.sh" >> "$LOG" 2>&1; then
    log "[-] patching failed!"
    exit 1
  fi
fi
log "patched model ready at $OUT"

TS=$(getprop init.svc.trackingservice)
TFS=$(getprop init.svc.trackingfidelityservice)

[ "$TS" = "running" ] && stop trackingservice
[ "$TFS" = "running" ] && stop trackingfidelityservice
sleep 2

umount "$MODEL" 2>/dev/null
if mount --bind "$OUT" "$MODEL"; then
  log "bind mount successful! : $OUT -> $MODEL ($(md5sum "$MODEL" | cut -d' ' -f1))"
else
  log "[-] bind mount failed! restarting the services with the original model..."
fi

[ "$TS" = "running" ] && start trackingservice
[ "$TFS" = "running" ] && start trackingfidelityservice
sleep 5
stop trackingservice 2>/dev/null
stop trackingfidelityservice 2>/dev/null
sleep 2
umount "$MODEL" 2>/dev/null
mount --bind "$OUT" "$MODEL"
start trackingservice
start trackingfidelityservice
sleep 3
log "done. (trackingservice was: $TS, trackingfidelityservice was: $TFS; now mounted: $(md5sum "$MODEL" | cut -d' ' -f1))"