@echo off
rem Keeps the sync scheduler running: restarts it automatically if it crashes.
rem Started from the Windows Startup folder - see SETUP_NEW_USER.md.
rem
rem STOP_FLAG: та же схема, что у core\webapp\supervisor.bat (см. комментарий
rem там) — пишется кнопкой "Остановить всё" в UI (POST /admin/stop_all).
rem Без этой проверки process kill из UI просто перезапустится через 5с.

cd /d "%~dp0"

:loop
if exist "STOP_FLAG" (
    echo [%date% %time%] STOP_FLAG found, exiting supervisor >> "..\..\logs\scheduler_supervisor.log"
    del "STOP_FLAG"
    goto :eof
)
echo [%date% %time%] Starting scheduler >> "..\..\logs\scheduler_supervisor.log"
python run.py >> "..\..\logs\scheduler_out.log" 2>> "..\..\logs\scheduler_err.log"
echo [%date% %time%] Scheduler exited, restarting in 5s >> "..\..\logs\scheduler_supervisor.log"
timeout /t 5 /nobreak >nul
goto loop
