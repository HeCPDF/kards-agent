@echo off
rem Resident listener (tools\live_session.py). Start the game FIRST, then this.
rem Commands go through <data>\live\live_cmd.txt, results in live_log.txt (see base\paths.py).
rem Stop it with the "quit" command or the control panel; do not force-kill it while a match is running.
rem Interpreter: %KARDS_PYTHON% > .venv\Scripts\python.exe > python on PATH.
setlocal
cd /d "%~dp0"
set "PY=%KARDS_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" tools\live_session.py %*
if errorlevel 1 pause
endlocal
