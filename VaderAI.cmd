@echo off
setlocal
cd /d "%~dp0"
if exist ".venv-desktop\Scripts\pythonw.exe" (
    start "" ".venv-desktop\Scripts\pythonw.exe" -m vader --desktop
) else (
    python -m vader --desktop
    if errorlevel 1 pause
)
