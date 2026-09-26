@echo off
REM Double-click this to switch the USB-connected Quest Pro to wireless (ADB over Wi-Fi).
REM It runs the PowerShell step with the execution policy bypassed and keeps this
REM window open so you can read the result or any error.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable-quest-wireless.ps1"
echo.
echo ============================================================
echo If you see "WIRELESS_ADB_READY ..." above, it worked - you can
echo unplug USB and press "Connect wirelessly" in the hub.
echo If you see an error, connect the headset by USB, put it on, and
echo accept the USB debugging + Magisk superuser prompts, then retry.
echo ============================================================
pause
