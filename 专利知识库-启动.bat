@echo off
rem Change to the script's own directory (works from any drive / any location)
cd /d "%~dp0"
echo Starting patent-kb service + watchdog + OCR driver ...
echo URL: http://127.0.0.1:8322
echo.
.venv\Scripts\python.exe scripts\restore.py
echo.
echo You can close this window; background processes keep running.
echo Re-run this script any time to restore after a crash or session restart.
pause
