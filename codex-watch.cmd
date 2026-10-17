@echo off
setlocal
set "PYTHONUTF8=1"
py -3 "%~dp0src\codex_desktop_weixin\cli.py" %*
exit /b %errorlevel%
