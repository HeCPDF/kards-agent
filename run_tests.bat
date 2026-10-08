@echo off
rem Offline test suite (no game needed). Optional argument = keyword filter, e.g. run_tests.bat seat
rem Interpreter: %KARDS_PYTHON% > .venv\Scripts\python.exe > python on PATH.
setlocal
cd /d "%~dp0"
set "PY=%KARDS_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" tests\run_all.py %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
