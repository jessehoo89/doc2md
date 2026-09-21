@echo off
rem  ---------------------------------------------------------------------------
rem   Cloud OCR self-check: backend connectivity + credentials in effect.
rem   With an argument, that argument is passed straight to doc2md instead, e.g.
rem      云端OCR自检.bat status
rem  ---------------------------------------------------------------------------
setlocal
title Cloud OCR self-check

call "%~dp0_python.bat"

if "%PYOK%"=="0" goto nopy
if not exist "%ROOT%doc2md\__main__.py" goto nopkg

cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if "%~1"=="" goto default

echo.
echo ### doc2md %*
echo.
"%PY%" -m doc2md %*
goto done

:default
echo.
echo ### 1/2  backend connectivity
echo.
"%PY%" -m doc2md ping
echo.
echo ### 2/2  credentials in effect
echo.
"%PY%" -m doc2md env
goto done

:nopy
echo.
echo   [ERROR] No usable Python interpreter found.
echo   Create the venv first:  python -m venv "%ROOT%.venv"
echo   Then install deps:      "%ROOT%.venv\Scripts\python.exe" -m pip install -r requirements.txt
echo   Or set DOC2MD_PYTHON to an existing interpreter.
goto end

:nopkg
echo.
echo   [ERROR] doc2md package not found under:
echo     %ROOT%
echo   Run this bat from inside the repository root.
goto end

:done
:end
echo.
pause
endlocal
exit /b 0
