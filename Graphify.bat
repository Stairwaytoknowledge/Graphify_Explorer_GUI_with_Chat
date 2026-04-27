@echo off
REM Graphify Explorer - simple launcher (use Graphify.vbs for no-console).
pushd "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Graphify is not installed yet. Run Install-Windows.bat first.
    pause
    popd
    exit /b 1
)
REM CI / autoquit mode: run synchronously so the harness can wait on it.
if defined GRAPHIFY_TEST_AUTOQUIT (
    ".venv\Scripts\python.exe" graphify_gui.py
    set RC=%errorlevel%
    popd
    exit /b %RC%
)
start "" ".venv\Scripts\pythonw.exe" graphify_gui.py
popd
exit /b 0
