@echo off
rem ============================================================================
rem  Shared Python interpreter detection.  NOT meant to be double-clicked.
rem
rem  Callers:   call "%~dp0_python.bat"
rem
rem  Returns (no setlocal, so the variables survive into the caller):
rem      ROOT  = repository root, with a trailing backslash
rem      PY    = interpreter to use
rem      PYOK  = 1 if an interpreter was found, otherwise 0
rem
rem  Lookup order:
rem     1. %DOC2MD_PYTHON%                    explicit override, wins outright
rem     2. <root>\.venv\Scripts\python.exe    recommended location
rem     3. <root>\venv\Scripts\python.exe     plain venv name
rem     4. first `python` on PATH that really executes
rem        (the Microsoft Store stub is rejected: exit code 9009, zero output)
rem ============================================================================

set "ROOT=%~dp0"
set "PY="
set "PYOK=0"

if defined DOC2MD_PYTHON if exist "%DOC2MD_PYTHON%" set "PY=%DOC2MD_PYTHON%"

if not defined PY if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"
if not defined PY if exist "%ROOT%venv\Scripts\python.exe"  set "PY=%ROOT%venv\Scripts\python.exe"

if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY (
      "%%i" -c "import sys" >nul 2>nul
      if not errorlevel 1 set "PY=%%i"
    )
  )
)

if defined PY set "PYOK=1"
exit /b 0
