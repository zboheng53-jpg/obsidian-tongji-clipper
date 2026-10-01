@echo off
setlocal
set "SCRIPT_DIR=%~dp0.."
cd /d "%SCRIPT_DIR%"

if defined TONGJI_CLIPPER_PYTHON (
    "%TONGJI_CLIPPER_PYTHON%" -m tongji_clipper.native_host.bridge
) else if exist "%SCRIPT_DIR%\.venv\Scripts\python.exe" (
    "%SCRIPT_DIR%\.venv\Scripts\python.exe" -m tongji_clipper.native_host.bridge
) else if exist "%SCRIPT_DIR%\.validation-venv\Scripts\python.exe" (
    "%SCRIPT_DIR%\.validation-venv\Scripts\python.exe" -m tongji_clipper.native_host.bridge
) else (
    python -m tongji_clipper.native_host.bridge
)
