@echo off
rem ---------------------------------------------------------------------------
rem run-analytics dashboard launcher (dashboard with the built-in tool rail).
rem
rem ASCII-only on purpose: cmd.exe reads this file with the OEM codepage and any
rem non-ASCII byte would garble (same rule as the other .cmd files here).
rem
rem What this starts:
rem   tools\serve.py -- one local process that both serves the dashboard AND
rem   exposes the import-pipeline API (/api/status, /api/refresh, /api/login,
rem   /api/cleanup, /api/versions, /api/rollback).
rem
rem   2026-09-27: serve.py now does three things this .cmd used to do:
rem     * picks its own free port (8917+, occupied -> next)  -- no netstat loop
rem     * prints the banner (real URL + token)               -- one source only
rem     * opens the browser once the port is BOUND           -- see --open-browser
rem   The old PowerShell "poll the URL every 250ms then Start-Process" step is
rem   gone. It cost a 1-3s cold start AND could open a SECOND tab, because the
rem   apex-console launcher opened the dashboard as well.
rem
rem Two launch paths share this one file (the launcher is the single truth):
rem   * double-click HERE        -> console window stays open, close it to stop.
rem   * apex-console "run"       -> same file, started with CREATE_NO_WINDOW, so
rem     nothing black ever flashes on screen; stop/restart via the console button.
rem
rem Pure static path stays available: double-click index.html (no process at all;
rem the tool rail degrades to grey buttons, the dashboard itself is unaffected).
rem
rem Nothing is exposed to the network (bound to 127.0.0.1 + a random per-run
rem token injected into the page).
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0"
set "ENTRY=tools\serve.py"

if not exist "%ROOT%%ENTRY%" goto noentry

rem --- use this project's isolated Python environment ------------------------
set "PY="
set "CAND=%ROOT%.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto havepy
goto noenv

:havepy
rem --- leave a trail in %TEMP%\run-analytics\ so a launcher can tell what happened ---
rem The dashboard's own launcher (apex-console runner-lab) starts THIS file detached and
rem only knows the pid/port it scraped from the bridge itself. When something goes wrong
rem later, "who started it, with which interpreter" must be answerable without guessing.
rem The port/URL/token are printed by serve.py below, not here.
set "RUNDIR=%TEMP%\run-analytics"
if not exist "%RUNDIR%" mkdir "%RUNDIR%"
> "%RUNDIR%\launcher.log" echo started %DATE% %TIME% pid=%RANDOM%
>>"%RUNDIR%\launcher.log" echo   root=%ROOT%
>>"%RUNDIR%\launcher.log" echo   py=%PY%

rem --open-browser: the bridge opens the dashboard itself, right after its port
rem is bound -- so the page is reachable the moment the browser asks for it.
"%PY%" "%ROOT%%ENTRY%" --open-browser
goto end

:noenv
echo [ERROR] run-analytics local Python environment is not ready.
echo         Run setup.ps1 in this project folder first.
echo         Workaround: double-click index.html for the static dashboard.
pause
exit /b 1

:noentry
echo [ERROR] %ENTRY% was not found under:
echo         %ROOT%
pause
exit /b 1

:end
endlocal
