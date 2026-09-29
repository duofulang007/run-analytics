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
rem Use this project's Python so all launchers share one isolated environment.
set "CAND=%ROOT%.venv\Scripts\pythonw.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run

set "CAND=%ROOT%.venv\Scripts\python.exe"
if exist "%CAND%" set "PY=%CAND%"
if defined PY goto run
goto noenv

:run
start "" "%PY%" "%GUI%" %*
exit /b 0

:missing
echo [ERROR] tools\import_gui.py was not found:
echo         %GUI%
echo Make sure this file still sits in the run-analytics project root.
pause
exit /b 1

:noenv
echo [ERROR] run-analytics local Python environment is not ready.
echo         Run setup.ps1 in this project folder first.
pause
exit /b 1
