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
set "CAND=%ROOT%.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run
goto noenv

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

:noenv
echo [ERROR] run-analytics local Python environment is not ready.
echo         Run setup.ps1 in this project folder first.
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1
