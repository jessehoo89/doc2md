@echo off
rem ============================================================================
rem  Build doc2md into standalone Windows executables.
rem  Output:  <root>\dist\doc2md\doc2md.exe       console menu + CLI
rem           <root>\dist\doc2md\doc2md-gui.exe   graphical interface
rem
rem  Both exes share ONE _internal folder, so shipping the GUI costs only a few
rem  extra MB. Keep the WHOLE dist\doc2md folder together when distributing --
rem  an exe alone will not run.
rem
rem  WHY .venv-gui IS PREFERRED
rem    The GUI needs tkinter, and PyInstaller can only bundle what the build
rem    interpreter actually has. The trimmed managed Python 3.13 here has no
rem    tkinter, so building with it would produce a GUI exe that crashes on
rem    startup (with no error at build time). .venv-gui is a full CPython.
rem ============================================================================
cd /d "%~dp0"

if exist "%~dp0.venv-gui\Scripts\python.exe" set "DOC2MD_PYTHON=%~dp0.venv-gui\Scripts\python.exe"

call "%~dp0_python.bat"
if not "%PYOK%"=="1" goto :nopython

echo Using interpreter: %PY%
echo.

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :notkinter

rem --- step 1: make sure PyInstaller is present -------------------------------
"%PY%" -c "import PyInstaller" >nul 2>nul
if not errorlevel 1 goto :build

echo [1/2] Installing PyInstaller ...
"%PY%" -m pip install pyinstaller --disable-pip-version-check
if errorlevel 1 goto :pipfail

:build
echo [2/2] Building (this takes a few minutes) ...
"%PY%" -m PyInstaller doc2md.spec --noconfirm
if errorlevel 1 goto :buildfail

echo.
echo ============================================================
echo  Done.
echo.
echo  doc2md.exe      : double-click for the console menu / CLI
echo  doc2md-gui.exe  : double-click for the graphical interface
echo.
echo  Folder : %ROOT%dist\doc2md
echo.
echo  Next steps:
echo    1. Copy config.example.json to config.json in that folder
echo       (or just run either exe once -- it creates it automatically).
echo    2. Put your .env with OCR tokens next to the exes.
echo    3. Distribute the whole dist\doc2md folder, not just one exe.
echo.
echo  Note: .doc / .xls still need WPS or MS Office on the target
echo        machine, and local RapidOCR needs its own interpreter.
echo ============================================================
pause
exit /b 0

:notkinter
echo.
echo [ERROR] This interpreter has no tkinter.
echo         Interpreter: %PY%
echo.
echo         PyInstaller can only bundle what the build interpreter has, so
echo         doc2md-gui.exe would crash on startup. Build with a FULL Python:
echo             python -m venv .venv-gui
echo             .venv-gui\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
echo.
pause
exit /b 1

:nopython
echo.
echo [ERROR] No usable Python interpreter found.
echo         python -m venv .venv
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1

:pipfail
echo.
echo [ERROR] Failed to install PyInstaller. Check network or proxy.
echo.
pause
exit /b 1

:buildfail
echo.
echo [ERROR] Build failed. Scroll up for the reason.
echo.
pause
exit /b 1
