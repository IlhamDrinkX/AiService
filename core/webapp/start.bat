@echo off
rem Starts the AI Service System web UI.
rem First run creates a venv and installs dependencies (a minute or two), then it's fast.

cd /d "%~dp0"

if not exist venv (
    echo First run: creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Python not found. Install Python 3.10+ and add it to PATH: https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

echo Checking dependencies...
venv\Scripts\pip install -q -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies - see the error above.
    pause
    exit /b 1
)

echo.
echo Starting server in a separate window (closing it stops the web UI)...
start "AI Service System - web UI" venv\Scripts\python -m uvicorn main:app --port 9000

timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:9000
