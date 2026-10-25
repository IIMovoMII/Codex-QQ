@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -X utf8 tools\install_app.py %*
    if not errorlevel 1 goto done
)
where python >nul 2>nul
if not errorlevel 1 (
    python -X utf8 tools\install_app.py %*
    if not errorlevel 1 goto done
)
echo Python 3.11+ is required. Install it from https://www.python.org/downloads/windows/ and try again.
popd
if "%~1"=="" pause
exit /b 1
:done
set "setup_exit=%errorlevel%"
popd
if "%~1"=="" pause
exit /b %setup_exit%
