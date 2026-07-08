@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt > "%TEMP%\random_ask_pip.log" 2>&1
if errorlevel 1 (
    echo Dependency install failed. See "%TEMP%\random_ask_pip.log"
    pause
    exit /b 1
)

for /f "delims=" %%P in ('python -c "import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))"') do set "PYTHONW=%%P"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

start "" "%PYTHONW%" "%~dp0random_ask.py"
exit /b 0
