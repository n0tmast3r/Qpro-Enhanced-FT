MODEL=/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/bolt/bolt.ptl

umount "$MODEL" 2>/dev/null

stop trackingservice 2>/dev/null
stop trackingfidelityservice 2>/dev/null
sleep 1
start trackingservice 2>/dev/null
start trackingfidelityservice 2>/dev/null

rm -f /data/local/tmp/bolt_patched.ptl
rm -f /data/local/tmp/model_patcher.log

echo "Model has been restored and tracking service has been restarted!"
