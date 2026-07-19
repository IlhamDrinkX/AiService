@echo off
rem Checks the OpenRouter connection. Creates the venv on first run, then just checks.

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

if not exist .env (
    echo No .env found - copy .env.example to .env and put your OPENROUTER_API_KEY in it.
    pause
    exit /b 1
)

venv\Scripts\python healthcheck.py
pause
