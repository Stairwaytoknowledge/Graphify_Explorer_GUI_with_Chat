@echo off
REM Graphify Explorer launcher (use Graphify.vbs for no-console).
REM
REM Order of preference:
REM   1. .venv\Scripts\pythonw.exe (the venv stub)
REM   2. The venv's "home" python (when WDAC / AppLocker blocks the stub)
REM   3. pythonw on PATH (system install)
REM graphify_gui.py self-bootstraps the venv site-packages, so any of
REM these interpreters can load it.
setlocal enableextensions enabledelayedexpansion
pushd "%~dp0"

set "GUI=%~dp0graphify_gui.py"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "PYVENV_CFG=%~dp0.venv\pyvenv.cfg"

REM --- CI / autoquit: run synchronously through whichever python works ---
if defined GRAPHIFY_TEST_AUTOQUIT (
    call :find_python PY_OK
    if not defined PY_OK (
        echo Graphify: no usable python interpreter.
        popd
        exit /b 2
    )
    "!PY_OK!" "%GUI%"
    set RC=!errorlevel!
    popd
    exit /b !RC!
)

REM --- 1. venv stub --------------------------------------------------
if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" "%GUI%"
    if not errorlevel 1 goto :ok
)

REM --- 2. venv home interpreter (parsed from pyvenv.cfg) -------------
if exist "%PYVENV_CFG%" (
    for /f "tokens=1,* delims==" %%a in ('type "%PYVENV_CFG%" ^| findstr /b /i "home"') do (
        set "HOME_DIR=%%b"
        set "HOME_DIR=!HOME_DIR: =!"
    )
    if defined HOME_DIR (
        if exist "!HOME_DIR!\pythonw.exe" (
            start "" "!HOME_DIR!\pythonw.exe" "%GUI%"
            if not errorlevel 1 goto :ok
        )
    )
)

REM --- 3. system pythonw on PATH ------------------------------------
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw "%GUI%"
    if not errorlevel 1 goto :ok
)

echo Graphify could not start. No usable python interpreter was found.
echo Run Install-Windows.bat first, or open a terminal here and try:
echo     python graphify_gui.py
popd
exit /b 1

:ok
popd
exit /b 0


REM --- helpers ------------------------------------------------------
:find_python
REM %1 = name of variable to set to a usable python.exe path
setlocal
set "OUT="
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" >nul 2>&1
    if not errorlevel 1 set "OUT=%VENV_PY%"
)
if not defined OUT if exist "%PYVENV_CFG%" (
    for /f "tokens=1,* delims==" %%a in ('type "%PYVENV_CFG%" ^| findstr /b /i "home"') do (
        set "HD=%%b"
        set "HD=!HD: =!"
    )
    if defined HD (
        if exist "!HD!\python.exe" set "OUT=!HD!\python.exe"
    )
)
if not defined OUT (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('where python') do (
            if not defined OUT set "OUT=%%p"
        )
    )
)
endlocal & set "%~1=%OUT%"
goto :eof
