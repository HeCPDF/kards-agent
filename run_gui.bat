@echo off
rem Control panel. Works in the dev layout and the released layout (everything is relative to this file).
rem Interpreter: %KARDS_PYTHON% > .venv\Scripts\python.exe > python on PATH.
setlocal
cd /d "%~dp0"
set "PY=%KARDS_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" -m gui.app %*
if errorlevel 1 pause
endlocal
