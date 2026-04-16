@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

set "PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] .venv\Scripts\python.exe not found.
    echo Create the virtual environment and install dependencies first.
    echo Example:
    echo   py -3 -m venv .venv
    echo   .venv\Scripts\pip install -r pc_app\requirements.txt
    pause
    exit /b 1
)

"%PYTHON%" -m pc_app.main
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Program exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
