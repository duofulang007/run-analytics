@echo off
rem ---------------------------------------------------------------------------
rem run-analytics: preview old Xiaomi export packages and processed-mail state.
rem Default is read-only preview. Pass --yes only after reviewing the list.
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title run-analytics cleanup

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%tools\cleanup.py"
if not exist "%SCRIPT%" goto missing

set "PY="
set "CAND=%ROOT%..\apex-console\backend\.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run

where py >nul 2>&1 && set "PY=py -3"
if defined PY goto run
where python >nul 2>&1 && set "PY=python"
if defined PY goto run

if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if defined PY goto run
goto nopython

:run
set "PYTHONIOENCODING=utf-8"
echo Interpreter: %PY%
echo.
%PY% "%SCRIPT%" %*
set "SCRIPT_EXIT=%ERRORLEVEL%"
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b %SCRIPT_EXIT%
echo.
echo Exit code: %SCRIPT_EXIT%
echo.
pause
exit /b %SCRIPT_EXIT%

:missing
echo [ERROR] tools\cleanup.py was not found:
echo         %SCRIPT%
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1

:nopython
echo [ERROR] No Python 3 found. Install Python 3 or add it to PATH.
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1
