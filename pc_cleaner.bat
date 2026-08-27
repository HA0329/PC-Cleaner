@echo off
rem pc-junk-cleaner launcher: double-click to run; auto jumps to project root
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  pause
  exit /b 1
)
python -m pc_cleaner %*
endlocal
