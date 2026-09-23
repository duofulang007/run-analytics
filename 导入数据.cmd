@echo off
rem ---------------------------------------------------------------------------
rem run-analytics import window launcher.
rem ASCII-only on purpose: cmd.exe reads this file with the OEM codepage, so any
rem non-ASCII byte would garble. All paths are expanded at runtime via %~dp0.
rem Double-click it, or drop the MiFitness zip straight onto this file.
rem ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0"
set "GUI=%ROOT%tools\import_gui.py"
if not exist "%GUI%" goto missing

set "PY="
rem run-analytics lives next to apex-console (D:\Projects\<name>), so the shared
rem venv is at ..\apex-console\backend\.venv. PATH pythonw is the last resort.
set "CAND=%ROOT%..\apex-console\backend\.venv\Scripts\pythonw.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run

set "CAND=%ROOT%..\apex-console\backend\.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run

for %%P in (pythonw.exe) do set "PY=%%~$PATH:P"
if defined PY goto run
goto nopython

:run
start "" "%PY%" "%GUI%" %*
exit /b 0

:missing
echo [ERROR] tools\import_gui.py was not found:
echo         %GUI%
echo Make sure this file still sits in the run-analytics project root.
pause
exit /b 1

:nopython
echo [ERROR] No Python runtime with tkinter was found.
echo         Expected one of:
echo           ..\apex-console\backend\.venv\Scripts\pythonw.exe
echo           pythonw.exe on PATH
pause
exit /b 1
