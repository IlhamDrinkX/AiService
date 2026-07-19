@echo off
cd /d "%~dp0"
venv\Scripts\python.exe sync.py >> "..\..\logs\tracker_sync.log" 2>&1
