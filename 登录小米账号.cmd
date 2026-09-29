@echo off
rem ---------------------------------------------------------------------------
rem run-analytics: open the Xiaomi sign-in page, tick the agreement, sign in.
rem
rem Scope is DELIBERATELY narrow: sign-in only. The Privacy Center, the SMS
rem second factor and the export request are all done by hand afterwards --
rem those are Xiaomi's sensitive-operation checks and Duofulang explicitly wants
rem it that way ("do the login for me; I'll do the privacy center myself").
rem
rem Why it is still worth a double-click: it uses the SAME browser profile as
rem the downloader (local/browser-profile), so the login state lives in exactly
rem one place. Sign in once here and every later download reuses it.
rem
rem ASCII-only on purpose: cmd.exe reads this file with the OEM codepage, so any
rem non-ASCII byte would garble. All paths are expanded at runtime via %~dp0.
rem
rem Double-click for the normal flow. Arguments are passed through:
rem     --qr              scan a QR code instead of typing a password
rem     --no-fill         tick the agreement and fill the account only
rem     --save-password   store the password DPAPI-encrypted (local only)
rem     --goto-only       already signed in: just open the window
rem     --probe           dump the page structure (for troubleshooting)
rem ---------------------------------------------------------------------------
setlocal
chcp 65001 >nul
title run-analytics xiaomi sign-in

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%tools\xiaomi_login.py"
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
rem -u = unbuffered. The dashboard's tool rail reads this process through a pipe,
rem and Python block-buffers stdout in that case -- the "waiting for you to sign
rem in" lines would never reach the page. See the mail-import launcher for the same note.
%PY% -u "%SCRIPT%" %*
set "SCRIPT_EXIT=%ERRORLEVEL%"
if /i "%RUN_ANALYTICS_NO_PAUSE%"=="1" exit /b %SCRIPT_EXIT%
echo.
echo Exit code: %SCRIPT_EXIT%
echo.
pause
exit /b %SCRIPT_EXIT%

:missing
echo [ERROR] tools\xiaomi_login.py was not found:
echo         %SCRIPT%
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
