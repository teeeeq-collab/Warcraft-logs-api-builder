@echo off
REM ===================================================================
REM  Windows setup for the Warcraft Logs Mythic+ collector.
REM
REM  Double-click this file, or run it from a terminal. It will:
REM    1. find your Python installation
REM    2. create a private virtual environment in .venv
REM    3. install the project into it
REM    4. create .env from .env.example if you do not have one yet
REM    5. run config-check to prove the install works
REM
REM  It does NOT need your Warcraft Logs credentials and makes no
REM  network calls to Warcraft Logs. Safe to re-run at any time.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   Warcraft Logs Mythic+ collector - Windows setup
echo ============================================================
echo.
echo Project folder: %CD%
echo.

REM --- Step 1: locate Python -----------------------------------------
set PYCMD=
py -3 --version >nul 2>&1 && set PYCMD=py -3
if "%PYCMD%"=="" (
    python --version >nul 2>&1 && set PYCMD=python
)
if "%PYCMD%"=="" (
    echo [FAILED] No Python installation found.
    echo.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/
    echo IMPORTANT: on the first installer screen, tick
    echo   "Add python.exe to PATH"
    echo before clicking Install.
    echo.
    echo Then close this window and double-click this file again.
    goto :done
)

echo [1/5] Found Python using: %PYCMD%
%PYCMD% --version

REM --- Step 2: create the virtual environment ------------------------
if exist ".venv\Scripts\python.exe" (
    echo [2/5] Virtual environment already exists, reusing it.
) else (
    echo [2/5] Creating virtual environment in .venv ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [FAILED] Could not create the virtual environment.
        goto :done
    )
)

REM --- Step 3: install the project ----------------------------------
echo [3/5] Installing the project ^(this may take a minute^) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -e . --quiet
if errorlevel 1 (
    echo [FAILED] Installation failed. Scroll up for the error text.
    goto :done
)
echo       Installed.

REM --- Step 4: create .env ------------------------------------------
if exist ".env" (
    echo [4/5] .env already exists, leaving it alone.
) else (
    copy ".env.example" ".env" >nul
    echo [4/5] Created .env from the template.
    echo       You still need to put your Client ID and Secret in it.
)

REM --- Step 5: prove it works ---------------------------------------
echo [5/5] Running config-check ...
echo.
".venv\Scripts\wclmplus.exe" config-check

echo.
echo ============================================================
echo   Setup finished.
echo ============================================================
echo.
echo Next steps:
echo   1. Put your Warcraft Logs Client ID and Secret in the .env file.
echo      Run this to open it:   notepad .env
echo   2. Then check they work:  .venv\Scripts\wclmplus.exe auth-check
echo.
echo Your Client Secret stays in .env on this computer only.
echo .env is ignored by git and is never uploaded anywhere.
echo.

:done
echo.
pause
endlocal
