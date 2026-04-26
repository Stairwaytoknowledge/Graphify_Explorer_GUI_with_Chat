@echo off
REM Graphify Explorer - simple launcher (use Graphify.vbs for no-console).
pushd "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Graphify is not installed yet. Run Install-Windows.bat first.
    pause
    popd
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" graphify_gui.py
popd
exit /b 0
