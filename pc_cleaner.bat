@echo off
rem pc-junk-cleaner launcher: double-click to run; auto jumps to project root
rem NOTE: keep the window open with "pause" so the result/errors are visible
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  echo.
  pause >nul
  exit /b 1
)
python -m pc_cleaner %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Finished (exit code %EXIT_CODE%). Press any key to close this window...
pause >nul
exit /b %EXIT_CODE%
