@echo off
rem Keeps the web UI running: restarts it automatically if it crashes or is killed.
rem Registered as a Scheduled Task ("At log on") - see SETUP_NEW_USER.md.
rem
rem STOP_FLAG: written by the "Stop" button in the UI (POST /admin/stop) when
rem the user wants the app to actually stay off, not just restart. Checked at
rem the top of every loop iteration - without this, killing the python process
rem from the UI would just get it relaunched 5s later by this same loop.

cd /d "%~dp0"

:loop
if exist "STOP_FLAG" (
    echo [%date% %time%] STOP_FLAG found, exiting supervisor >> "..\..\logs\webapp_supervisor.log"
    del "STOP_FLAG"
    goto :eof
)
echo [%date% %time%] Starting web UI >> "..\..\logs\webapp_supervisor.log"
venv\Scripts\python.exe -m uvicorn main:app --port 9000 >> "..\..\logs\webapp_out.log" 2>> "..\..\logs\webapp_err.log"
echo [%date% %time%] Web UI exited, restarting in 5s >> "..\..\logs\webapp_supervisor.log"
timeout /t 5 /nobreak >nul
goto loop
