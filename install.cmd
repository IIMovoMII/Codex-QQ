@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 goto run_py
)
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
    if not errorlevel 1 goto run_python
)
echo Python 3.11+ is required. Install it from https://www.python.org/downloads/windows/ and try again.
popd
if "%~1"=="" pause
exit /b 1
:run_py
py -3 -X utf8 tools\install_app.py %*
goto done
:run_python
python -X utf8 tools\install_app.py %*
goto done
:done
set "setup_exit=%errorlevel%"
popd
if "%~1"=="" pause
exit /b %setup_exit%
