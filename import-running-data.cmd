@echo off
setlocal
title run-analytics data import

set "SCRIPT=%~dp0tools\import_zip.py"
if not exist "%SCRIPT%" (
  echo [ERROR] tools\import_zip.py not found next to this script.
  pause
  exit /b 1
)

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY (
  if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY (
  echo [ERROR] No Python 3 found. Install Python 3 or add it to PATH.
  pause
  exit /b 1
)

echo Interpreter: %PY%
echo.
%PY% "%SCRIPT%" %*
echo.
echo Exit code: %ERRORLEVEL%
pause
