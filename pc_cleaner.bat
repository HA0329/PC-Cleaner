@echo off
rem pc-junk-cleaner launcher: double-click to run; auto jumps to project root
rem Usage: pc_cleaner.bat [options]   (options passed to python -m pc_cleaner)
rem   --no-pause  : do not wait for a key press before closing (launcher-only flag)
setlocal
cd /d "%~dp0"

rem v0.9.3: force UTF-8 so Chinese/emoji do not crash on a cp936 console
set "PYTHONUTF8=1"

rem --- locate a Python 3.10+ interpreter ---
rem v0.9.3: prefer "py -3" (the Microsoft Store python stub breaks the version check)
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  echo.
  pause >nul
  exit /b 1
)

rem --- verify version ---
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ is required, but an older version was found.
  echo.
  pause >nul
  exit /b 1
)

rem --- split launcher-only --no-pause from the args forwarded to Python ---
rem v0.9.3: previously --no-pause was forwarded to Python -> "unrecognized arguments" + exit 2
set "ARGS="
set "NOPAUSE="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--no-pause" goto mark_nopause
set "ARGS=%ARGS% %~1"
shift
goto parse
:mark_nopause
set "NOPAUSE=1"
shift
goto parse
:parsed

%PY% -m pc_cleaner%ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Finished (exit code %EXIT_CODE%).
if not defined NOPAUSE pause >nul
exit /b %EXIT_CODE%
