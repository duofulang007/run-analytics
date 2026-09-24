@echo off
rem ---------------------------------------------------------------------------
rem run-analytics dashboard launcher (minimal shell).
rem
rem ASCII-only on purpose: cmd.exe reads this file with the OEM codepage and any
rem non-ASCII byte would garble (same rule as the other .cmd files here).
rem
rem Why a local http server at all, when shell\minimal.html opens fine by itself:
rem   file:// 下 iframe 是"不透明源"，外壳碰不到看板内部，禁选中 / 隐藏编辑类元素
rem   这类"像 App"的设置就注入不进去。http://127.0.0.1 下是同源，才能注入。
rem   只想看数据的话，直接双击 shell\minimal.html 就够了（秒开、无进程）。
rem
rem This window IS the server: keep it open while viewing, close it to stop.
rem Nothing is exposed to the network (--bind 127.0.0.1).
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0"
set "PAGE=shell/minimal.html"
set "PORT=8917"

if not exist "%ROOT%%PAGE%" goto nopage

rem --- pick a python (same candidates as the other launchers here) -----------
set "PY="
set "CAND=%ROOT%..\apex-console\backend\.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto havepy
for %%P in (python.exe) do set "PY=%%~$PATH:P"
if defined PY goto havepy
goto nopython

:havepy
rem --- find a free port (8917..8960) -----------------------------------------
:scan
if %PORT% GTR 8960 goto noport
netstat -an | findstr /c:":%PORT% " | findstr /i "LISTENING" >nul
if not errorlevel 1 (
  set /a PORT+=1
  goto scan
)

set "URL=http://127.0.0.1:%PORT%/%PAGE%"

echo.
echo   run-analytics  -  dashboard
echo   -----------------------------
echo   URL   : %URL%
echo   Root  : %ROOT%
echo   Keep this window open while viewing. Close it to stop the server.
echo.

rem --- open the browser once the port answers (hidden, no focus stealing) ----
start /min "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "Start-Sleep -Milliseconds 1000; Start-Process '%URL%'"

"%PY%" -m http.server %PORT% --bind 127.0.0.1 --directory "%ROOT%."
goto end

:nopython
echo [ERROR] No python.exe found.
echo         Tried: ..\apex-console\backend\.venv\Scripts\python.exe and PATH.
echo         Workaround: double-click shell\minimal.html directly
echo         (the dashboard shows fine; only the read-only injection is skipped).
pause
exit /b 1

:nopage
echo [ERROR] %PAGE% was not found under:
echo         %ROOT%
pause
exit /b 1

:noport
echo [ERROR] No free port in 8917-8960.
pause
exit /b 1

:end
endlocal
