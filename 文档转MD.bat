@echo off
rem  ---------------------------------------------------------------------------
rem   Document -> Markdown   ·   interactive menu
rem   Chinese prompts are printed by launcher.py; cmd's OEM code page would
rem   mangle them, so this file deliberately stays ASCII-only.
rem  ---------------------------------------------------------------------------
title Document to Markdown

call "%~dp0_python.bat"

if "%PYOK%"=="0" (
  echo.
  echo   [ERROR] No usable Python interpreter found.
  echo.
  echo   Create the virtual environment once, then run this file again:
  echo.
  echo       python -m venv "%ROOT%.venv"
  echo       "%ROOT%.venv\Scripts\python.exe" -m pip install -r requirements.txt
  echo.
  echo   Or point this variable at an existing interpreter:
  echo       set DOC2MD_PYTHON=C:\path\to\python.exe
  echo.
  pause
  exit /b 1
)

if not exist "%ROOT%launcher.py" (
  echo.
  echo   [ERROR] launcher.py not found under:
  echo     %ROOT%
  echo   Run this bat from inside the repository root.
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

"%PY%" "%ROOT%launcher.py" %*
if errorlevel 1 pause
