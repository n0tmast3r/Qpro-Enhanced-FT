@echo off
REM Double-click this to turn the headset's ADB back to USB-only (undo wireless).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0disable-quest-wireless.ps1"
echo.
pause
