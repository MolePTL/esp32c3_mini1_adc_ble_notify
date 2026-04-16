@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

set "PYTHONW=%PROJECT_ROOT%.venv\Scripts\pythonw.exe"
set "PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"

if exist "%PYTHONW%" (
    start "" /D "%PROJECT_ROOT%" "%PYTHONW%" -m pc_app.main
    exit /b 0
)

if exist "%PYTHON%" (
    start "" /D "%PROJECT_ROOT%" "%PYTHON%" -m pc_app.main
    exit /b 0
)

echo [ERROR] .venv\Scripts\python.exe not found.
echo Create the virtual environment and install dependencies first.
echo Example:
echo   py -3 -m venv .venv
echo   .venv\Scripts\pip install -r pc_app\requirements.txt
pause
exit /b 1
