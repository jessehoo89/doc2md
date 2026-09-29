@echo off
rem ============================================================================
rem  Launch the graphical interface (gui.py).
rem
rem  WHY THIS FILE EXISTS
rem    The GUI needs a Python that has tkinter.  The WorkBuddy-managed Python
rem    3.13 on this machine is a trimmed build WITHOUT tkinter (no _tkinter.pyd
rem    at all), so the project's own .venv cannot run the GUI.  .venv-gui is a
rem    second environment built from a full CPython (uv-managed 3.11.15) which
rem    DOES ship tkinter -- that is the one we prefer here.
rem
rem  The file NAME is Chinese so it reads well when double-clicked from
rem  Explorer; the CONTENT stays pure ASCII so cmd's code page cannot garble it.
rem ============================================================================
cd /d "%~dp0"

if exist "%~dp0.venv-gui\Scripts\python.exe" set "DOC2MD_PYTHON=%~dp0.venv-gui\Scripts\python.exe"

call "%~dp0_python.bat"
if not "%PYOK%"=="1" goto :nopython

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :notkinter

echo.
echo Starting graphical interface ...
echo (A console window stays open behind it. Closing that window stops the program.)
echo.
"%PY%" "%~dp0gui.py"
exit /b 0

:notkinter
echo.
echo [ERROR] The interpreter found has no tkinter, so the GUI cannot start.
echo         Interpreter: %PY%
echo.
echo         Build the GUI environment once (takes a few minutes):
echo             python -m venv .venv-gui
echo             .venv-gui\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo         NOTE: plain "python" here is the Microsoft Store stub and will
echo         silently do nothing. Use a FULL CPython install instead.
echo.
pause
exit /b 1

:nopython
echo.
echo [ERROR] No usable Python interpreter found.
echo         python -m venv .venv-gui
echo         .venv-gui\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1
