@echo off
rem pc-junk-cleaner launcher: double-click to run; auto jumps to project root
rem Usage: pc_cleaner.bat [options]   (options passed to python -m pc_cleaner)
rem   --no-pause  : do not wait for a key press before closing
setlocal
cd /d "%~dp0"

rem --- locate a Python 3.10+ interpreter ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
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

%PY% -m pc_cleaner %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Finished (exit code %EXIT_CODE%).
for %%a in (%*) do if /i "%%~a"=="--no-pause" goto :done
pause >nul
:done
exit /b %EXIT_CODE%
