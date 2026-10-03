@echo off
rem ===========================================================================
rem  Build the single-file doc2md installer.
rem
rem  ASCII-only on purpose (cmd codepage safety).
rem
rem  Usage:
rem    <this-file>.bat                full build (app + payload + installer)
rem    <this-file>.bat --force        force-rebuild dist\doc2md as well
rem                                   (use this after editing gui.py / doc2md/)
rem    <this-file>.bat --skip-app     reuse existing dist\doc2md
rem    <this-file>.bat --clean        wipe build\ dist\ dist-installer\ first
rem    <this-file>.bat --no-installer only build the app (dist\doc2md)
rem
rem  Output: dist-installer\doc2md-安装程序.exe  (single-file installer)
rem ===========================================================================
setlocal enableextensions
cd /d "%~dp0"

set "PY="

if not exist ".venv-gui\Scripts\python.exe" goto :try_venv
".venv-gui\Scripts\python.exe" -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :try_venv
set "PY=.venv-gui\Scripts\python.exe"
goto :found

:try_venv
if not exist ".venv\Scripts\python.exe" goto :try_path
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :try_path
set "PY=.venv\Scripts\python.exe"
goto :found

:try_path
for %%P in (python.exe) do set "PY=%%~$PATH:P"
if not defined PY goto :nopython
"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :nopython
goto :found

:nopython
echo [ERROR] No Python interpreter with tkinter was found.
echo.
echo         Expected one of:
echo           .venv-gui\Scripts\python.exe   (the GUI / packaging env)
echo           .venv\Scripts\python.exe
echo           python.exe on PATH
echo.
echo         The bundled .venv is a trimmed build WITHOUT tkinter.
pause
exit /b 1

:found
echo ===========================================================================
echo   doc2md - build single-file installer
echo ===========================================================================
echo   Interpreter : %PY%
echo   Options     : %*
echo ===========================================================================
echo.

"%PY%" make_installer.py %*
if errorlevel 1 goto :failed

echo.
echo [DONE] Installer written to: dist-installer\
pause
exit /b 0

:failed
echo.
echo [FAILED] See the messages above.
pause
exit /b 1
