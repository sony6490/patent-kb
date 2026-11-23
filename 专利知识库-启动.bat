@echo off
rem 切到脚本所在目录（不依赖固定盘符，仓库可放任意位置）
cd /d "%~dp0"
echo Starting patent-kb ...
echo URL: http://127.0.0.1:8322
echo Close this window to stop the server.
echo.
uv run patentkb
echo.
echo Server stopped.
pause
