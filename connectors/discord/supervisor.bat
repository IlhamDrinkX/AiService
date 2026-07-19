@echo off
rem Keeps the Discord bot running: restarts it automatically if it crashes or is killed.
rem Registered as a Scheduled Task ("At log on") - see SETUP_NEW_USER.md.
rem
rem STOP_FLAG: та же схема, что у core\webapp\supervisor.bat (см. комментарий
rem там) — пишется кнопкой "Остановить всё" в UI (POST /admin/stop_all).
rem Без этой проверки process kill из UI просто перезапустится через 5с.

cd /d "%~dp0"

:loop
if exist "STOP_FLAG" (
    echo [%date% %time%] STOP_FLAG found, exiting supervisor >> "..\..\logs\discord_supervisor.log"
    del "STOP_FLAG"
    goto :eof
)
echo [%date% %time%] Starting Discord bot >> "..\..\logs\discord_supervisor.log"
venv\Scripts\python.exe bot.py >> "..\..\logs\discord_bot_out.log" 2>> "..\..\logs\discord_bot_err.log"
echo [%date% %time%] Discord bot exited, restarting in 5s >> "..\..\logs\discord_supervisor.log"
timeout /t 5 /nobreak >nul
goto loop
