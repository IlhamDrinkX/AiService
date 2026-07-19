@echo off
cd /d "%~dp0"
venv\Scripts\python.exe sync.py >> "..\..\logs\fibbee_sync.log" 2>&1
