@echo off
rem One-click launcher for the control panel.
rem First run: creates .venv and installs dependencies (needs network). Later runs: opens the panel directly.
rem ASCII only on purpose: cmd.exe mis-parses UTF-8 text in batch files.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto have_venv

where python >nul 2>nul
if errorlevel 1 goto no_python

echo [setup] creating .venv ...
python -m venv .venv
if errorlevel 1 goto venv_failed

echo [setup] installing dependencies: frida, numpy ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pip_failed

:have_venv
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tk

call "%~dp0run_gui.bat" %*
goto done

:no_python
echo.
echo [ERROR] Python 3.12 or newer was not found.
echo         Install it from https://www.python.org/downloads/
echo         During setup tick "Add python.exe to PATH" and keep "tcl/tk and IDLE".
echo         Then double-click this file again.
echo         See GUI-QUICKSTART.md for details.
goto fail

:venv_failed
echo.
echo [ERROR] Could not create the virtual environment .venv
goto fail

:pip_failed
echo.
echo [ERROR] pip install failed. Check the network connection and try again.
goto fail

:no_tk
echo.
echo [ERROR] This Python has no tkinter. Reinstall Python and keep "tcl/tk and IDLE".
goto fail

:fail
pause
exit /b 1

:done
endlocal
