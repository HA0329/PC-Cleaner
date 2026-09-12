@echo off
rem pc-junk-cleaner launcher: double-click to run; auto jumps to project root
rem Usage: pc_cleaner.bat [options]   (options passed to python -m pc_cleaner)
rem   --no-pause  : do not wait for a key press before closing (launcher-only flag)

rem v0.9.6: switch to UTF-8 so the Chinese startup hint renders on any console
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem v0.9.6: print feedback IMMEDIATELY after double-click, so the window never
rem sits there with only a blinking cursor while Python boots / AV scans files.
echo.
echo  正在启动 PC Junk Cleaner ...
echo.

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

rem --- split launcher-only --no-pause from the args forwarded to Python ---
rem v0.9.3: previously --no-pause was forwarded to Python -> "unrecognized arguments" + exit 2
rem v0.9.10: keep the ORIGINAL quoting. `%~1` strips the surrounding quotes, so
rem rebuilding with plain `%ARGS% %~1` turned a quoted path containing spaces
rem ("D:\My Dir\scan.json") into two separate arguments. Re-adding the quotes and
rem re-expanding %ARGS% (one extra parse pass) splits the string back into the
rem original argument boundaries and forwards them intact.
set "ARGS="
set "NOPAUSE="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--no-pause" goto mark_nopause
set ARGS=%ARGS% "%~1"
shift
goto parse
:mark_nopause
set "NOPAUSE=1"
shift
goto parse
:parsed

rem v0.9.6: launch Python ONCE. Old versions ran `python -c "version check"` (its
rem output was discarded with >nul, so the screen showed nothing) and then
rem `python -m pc_cleaner` -- two cold starts of the interpreter. With antivirus
rem (360 / Huorong / Defender ...) real-time scanning every file, each cold start
rem is slowed by seconds, adding up to a long "blinking cursor" window.
rem _launcher.py does the version check first and then enters the main program.
%PY% "%~dp0_launcher.py"%ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Finished (exit code %EXIT_CODE%).
if not defined NOPAUSE pause >nul
exit /b %EXIT_CODE%
