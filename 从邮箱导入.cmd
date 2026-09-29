@echo off
rem ---------------------------------------------------------------------------
rem run-analytics: fetch the export password from QQ Mail, then import the
rem package that is already on disk.
rem
rem The download itself is NOT automated on purpose: Xiaomi's download link is
rem bound to your login session, so only your browser can fetch the file.
rem This script therefore opens that page in your browser, waits for the file
rem to land in the download folder, imports it with the password from the mail,
rem and pops up a "you can look now" notification.
rem
rem ASCII-only on purpose: cmd.exe reads this file with the OEM codepage, so any
rem non-ASCII byte would garble. All paths are expanded at runtime via %~dp0.
rem Chinese output comes from the Python script, so the codepage is switched to
rem UTF-8 for the duration of this window.
rem
rem Double-click for the full flow (open page -> wait -> import -> notify).
rem To inspect instead, run it with an argument:
rem     --check     list candidate mails, import nothing
rem     --peek      dump the newest candidate's structure (secrets masked)
rem     --dry-run   say which package and how many passwords would be used
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title run-analytics mail import

set "ROOT=%~dp0"
set "FETCH=%ROOT%tools\fetch_from_mail.py"
if not exist "%FETCH%" goto missing

set "PY="
set "CAND=%ROOT%.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run
goto noenv

:run
set "PYTHONIOENCODING=utf-8"
rem Pass optional fetch_from_mail.py arguments through; successful imports stay in this console.
set "ARGS=%*"
echo Interpreter: %PY%
echo.
rem -u = unbuffered. Without it Python block-buffers stdout when it is a pipe
rem (which is how the dashboard's tool rail reads it), so every progress line --
rem including "still waiting for you to sign in" -- sat invisible in the buffer
rem until the process exited. Symptom: the progress card never moved.
%PY% -u "%FETCH%" %ARGS%
set "SCRIPT_EXIT=%ERRORLEVEL%"
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b %SCRIPT_EXIT%
echo.
echo Exit code: %SCRIPT_EXIT%
echo.
pause
exit /b %SCRIPT_EXIT%

:missing
echo [ERROR] tools\fetch_from_mail.py was not found:
echo         %FETCH%
echo Make sure this file still sits in the run-analytics project root.
echo.
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1

:noenv
echo [ERROR] run-analytics local Python environment is not ready.
echo         Run setup.ps1 in this project folder first.
echo.
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b 1
pause
exit /b 1
