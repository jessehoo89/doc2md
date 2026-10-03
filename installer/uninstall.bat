@echo off
rem ===========================================================================
rem  doc2md uninstaller
rem
rem  NOTE: this file is intentionally ASCII-only. Putting non-ASCII text in a
rem  .bat makes it garble under the default cmd codepage, so all user-facing
rem  strings below are English by design.
rem
rem  Flow:
rem    1. try to get administrator rights (the install dir is usually under
rem       C:\Program Files, which needs elevation to delete)
rem    2. copy ourselves to %TEMP% so the original can be deleted together with
rem       its own folder
rem    3. remove shortcuts, the registry entry, then the install directory
rem
rem  Usage:
rem    uninstall.bat                 interactive
rem    uninstall.bat /S              no trailing pause (for scripts)
rem ===========================================================================
setlocal enableextensions

rem %~4 == /NOTEMP : we are already the TEMP copy, go straight to work
if /i "%~4"=="/NOTEMP" goto :WORK
if /i "%~1"=="/GO" goto :FROM_GO

rem ---------------------------- entry A: normal launch ----------------------
net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] Administrator rights are required. Requesting elevation...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '/GO','%~dp0','%~1' -Verb RunAs" 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Could not get administrator rights.
        echo         Right-click this file and choose "Run as administrator".
        pause
    )
    exit /b
)
set "TARGET=%~dp0"
set "QUIET=%~1"
goto :DETACH

rem ---------------------------- entry B: re-launched elevated ---------------
:FROM_GO
set "TARGET=%~2"
set "QUIET=%~3"

:DETACH
if "%TARGET%"=="" set "TARGET=%~dp0"
copy /y "%~f0" "%TEMP%\doc2md-uninstall.bat" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Cannot copy the uninstaller to %TEMP% - aborting.
    pause
    exit /b 1
)
"%TEMP%\doc2md-uninstall.bat" /GO "%TARGET%" "%QUIET%" /NOTEMP
exit /b

rem ============================ real work ==================================
:WORK
set "TARGET=%~2"
set "QUIET=%~3"
if "%TARGET%"=="" set "TARGET=%~dp0"

cd /d "%TEMP%"

echo ===========================================================================
echo   Uninstalling doc2md
echo   Target: %TARGET%
echo ===========================================================================
echo.

echo [1/4] Stopping running instances...
taskkill /f /im doc2md.exe >nul 2>&1
taskkill /f /im doc2md-gui.exe >nul 2>&1

echo [2/4] Removing shortcuts...
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\doc2md*.lnk" >nul 2>&1
del /q "%USERPROFILE%\Desktop\doc2md*.lnk" >nul 2>&1
del /q "%PUBLIC%\Desktop\doc2md*.lnk" >nul 2>&1

echo [3/4] Removing the registry entry...
reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\doc2md" /f >nul 2>&1

echo [4/4] Removing files...
rd /s /q "%TARGET%" >nul 2>&1

if exist "%TARGET%" (
    echo.
    echo   [WARN] Some files are still locked and were not removed:
    echo          %TARGET%
    echo          Close doc2md, Explorer windows or editors pointing at that
    echo          folder, then delete it manually.
) else (
    echo   Done. doc2md has been removed.
)

del "%~f0" >nul 2>&1
if /i not "%QUIET%"=="/S" pause
exit /b 0
