@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Graphify Explorer - Installer

REM ----------------------------------------------------------------------
REM Double-click installer for Windows.
REM
REM What it does:
REM   1. Locates a Python 3.10+ interpreter (or `uv` if available).
REM   2. Creates an isolated virtual environment in `.venv` next to this file.
REM   3. Installs `graphifyy` (the upstream Graphify package) and dependencies.
REM   4. Generates `icon.ico`/`icon.png`.
REM   5. Creates a Desktop shortcut named "Graphify Explorer" with the icon.
REM ----------------------------------------------------------------------

pushd "%~dp0"

echo.
echo ============================================================
echo   Graphify Explorer  -  Windows installer
echo ============================================================
echo.

REM ---- Locate uv or python --------------------------------------------------
set "USE_UV="
where uv >nul 2>nul && set "USE_UV=1"

set "PY="
if defined USE_UV (
    echo [1/5] Found uv. Using it for venv + install.
) else (
    where py >nul 2>nul && set "PY=py -3"
    if not defined PY (
        where python >nul 2>nul && set "PY=python"
    )
    if not defined PY (
        echo ERROR: No Python 3.10+ found and `uv` is not installed.
        echo        Install Python from https://www.python.org/downloads/
        echo        or install uv from https://docs.astral.sh/uv/
        echo.
        pause
        popd
        exit /b 1
    )
    echo [1/5] Using %PY% to create the venv.
)

REM ---- Create venv ----------------------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo [2/5] Reusing existing .venv\
) else (
    if defined USE_UV (
        uv venv .venv --python 3.12 || uv venv .venv
    ) else (
        %PY% -m venv .venv
    )
    if errorlevel 1 (
        echo ERROR: failed to create .venv
        pause
        popd
        exit /b 1
    )
    echo [2/5] Created .venv\
)

REM ---- Install dependencies (prefer the locked file for reproducibility) --
REM    requirements.lock (universal, fully pinned by `uv pip compile`)
REM    -> deterministic install: every machine gets the exact same
REM       graphifyy/watchdog/numpy/pywebview versions plus their transitives.
REM    requirements.txt is the loose source, used only if the lock is
REM    missing (e.g. fresh checkout on an unsupported platform).
set "REQ_FILE=requirements.txt"
if exist "requirements.lock" set "REQ_FILE=requirements.lock"
echo [3/5] Installing dependencies from %REQ_FILE%...
set "INSTALL_RC=0"
if defined USE_UV (
    uv pip install --python ".venv\Scripts\python.exe" -r "%REQ_FILE%"
    set "INSTALL_RC=!errorlevel!"
) else (
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r "%REQ_FILE%"
    set "INSTALL_RC=!errorlevel!"
)
if not "!INSTALL_RC!"=="0" goto :install_failed

REM ---- Sanity check: pywebview must be importable ---------------------------
".venv\Scripts\python.exe" -c "import webview" >nul 2>nul
if errorlevel 1 goto :webview_failed

REM ---- Generate icon --------------------------------------------------------
echo [4/5] Generating icon...
".venv\Scripts\python.exe" make_icon.py
if errorlevel 1 (
    echo WARNING: icon generation failed; continuing without a custom icon.
)

REM ---- Create Desktop shortcut ---------------------------------------------
echo [5/5] Creating Desktop shortcut "Graphify Explorer"...
set "PS1=%TEMP%\graphify_make_shortcut.ps1"
> "%PS1%" echo $appDir = '%~dp0'
>> "%PS1%" echo $appDir = $appDir.TrimEnd('\')
>> "%PS1%" echo $desktop = [Environment]::GetFolderPath('Desktop')
>> "%PS1%" echo $shortcut = Join-Path $desktop 'Graphify Explorer.lnk'
>> "%PS1%" echo $target = Join-Path $appDir 'Graphify.vbs'
>> "%PS1%" echo $icon = Join-Path $appDir 'icon.ico'
>> "%PS1%" echo $sh = New-Object -ComObject WScript.Shell
>> "%PS1%" echo $lnk = $sh.CreateShortcut($shortcut)
>> "%PS1%" echo $lnk.TargetPath = $target
>> "%PS1%" echo $lnk.WorkingDirectory = $appDir
>> "%PS1%" echo $lnk.IconLocation = "$icon,0"
>> "%PS1%" echo $lnk.Description = 'Graphify Explorer - knowledge-graph GUI for any folder'
>> "%PS1%" echo $lnk.Save()
>> "%PS1%" echo Write-Host "Created: $shortcut"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
del /q "%PS1%" >nul 2>nul

echo.
echo ============================================================
echo   Install complete.
echo   - Double-click "Graphify Explorer" on your Desktop, or
echo     run Graphify.bat / Graphify.vbs in this folder.
echo ============================================================
echo.
if not defined CI if not defined NONINTERACTIVE pause
popd
endlocal
exit /b 0

REM =========================================================================
REM Error handlers (jumped to via `goto`). Using labels avoids the cmd
REM batch quirk where `(parens)` inside `if (...)` blocks confuse the
REM parser when the error message itself contains parentheses.
REM =========================================================================

:install_failed
echo.
echo ============================================================
echo   ERROR: dependency install failed.
echo ============================================================
echo.
echo What this means:
echo   At least one required package could not be installed. The GUI
echo   needs all of these to run:
echo     - graphifyy   ^(the upstream graph engine^)
echo     - watchdog    ^(used by `graphify watch`^)
echo     - numpy       ^(used by the Insights tab^)
echo     - pywebview   ^(powers the Interactive Graph window^)
echo.
echo How to fix on Windows:
echo   1. Make sure you have an internet connection ^(PyPI is needed^).
echo   2. Make sure Edge WebView2 is installed ^(it is on Windows 10+ by
echo      default; if it isn't, get it from
echo      https://developer.microsoft.com/microsoft-edge/webview2/ ^).
echo   3. Re-run this installer. If it still fails, scroll up and read
echo      the last few lines of pip's output - the missing package
echo      name is usually right above the traceback.
echo.
echo See README.md ^(Troubleshooting^) for more options.
echo.
if not defined CI if not defined NONINTERACTIVE pause
popd
endlocal
exit /b 1

:webview_failed
echo.
echo ============================================================
echo   ERROR: pywebview installed but cannot be imported.
echo ============================================================
echo   This usually means Edge WebView2 is missing. Install it from:
echo     https://developer.microsoft.com/microsoft-edge/webview2/
echo   then re-run this installer.
echo.
if not defined CI if not defined NONINTERACTIVE pause
popd
endlocal
exit /b 1
