@echo off
cd /d "D:\WorkBuddy\2026-11-18-15-20-21\patent-kb"
echo Starting patent-kb ...
echo URL: http://127.0.0.1:8322
echo Close this window to stop the server.
echo.
uv run patentkb
echo.
echo Server stopped.
pause
