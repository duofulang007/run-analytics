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
rem playwright lives in the shared venv next to apex-console; it is the only
rem interpreter on this machine known to have it, so it is tried first.
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
echo.
echo Exit code: %ERRORLEVEL%
echo.
pause
exit /b 0

:missing
echo [ERROR] tools\xiaomi_login.py was not found:
echo         %SCRIPT%
echo Make sure this file still sits in the run-analytics project root.
echo.
pause
exit /b 1

:nopython
echo [ERROR] No Python 3 found. Install Python 3 or add it to PATH.
echo         playwright is required; the shared venv is:
echo           ..\apex-console\backend\.venv\Scripts\python.exe
echo.
pause
exit /b 1
