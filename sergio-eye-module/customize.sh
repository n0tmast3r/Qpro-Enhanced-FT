SKIPUNZIP=0

ui_print "- *******************************"
ui_print "- Quest Pro Individual Eye Enabler"
ui_print "- by SergioMarquina"
ui_print "- *******************************"

ui_print "- patching the original model"
sh "$MODPATH/service.sh"
if [ $? -eq 0 ]; then
  ui_print "- model has been patched! Make sure to do the eye tracking calibration!"
else
  ui_print "! an error occured while patching the model! Please check /data/local/tmp/model_patcher.log"
fi
