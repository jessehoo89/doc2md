@echo off
rem  ---------------------------------------------------------------------------
rem   Document -> Markdown   ·   real-time watch mode
rem  ---------------------------------------------------------------------------
setlocal
title Document to Markdown - Watch Mode

call "%~dp0_python.bat"

if "%PYOK%"=="0" (
  echo.
  echo   [ERROR] No usable Python interpreter found.
  echo   Create the venv first:  python -m venv "%ROOT%.venv"
  echo   Then install deps:      "%ROOT%.venv\Scripts\python.exe" -m pip install -r requirements.txt
  echo   Or set DOC2MD_PYTHON to an existing interpreter.
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ================================================================================
echo    Real-time watch mode (auto convert new / modified files)
echo    Close this window or press Ctrl+C to stop.
echo ================================================================================
echo.

"%PY%" -m doc2md watch %*
echo.
echo Watch mode stopped.
pause
endlocal
