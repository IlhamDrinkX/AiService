@echo off
rem Ставит venv+зависимости для ВСЕХ модулей проекта одним прогоном (вместо
rem захода в каждую папку по отдельности), и запускает веб-морду.
rem Рассчитан на запуск из корня проекта (там же, где этот файл).
rem
rem Про авторизацию (Google/Discord/OpenRouter/трекер/etc.) — см. README в
rem каждом connectors\<name>\ и SETUP_NEW_USER.md, а также /help в самом
rem приложении после запуска. Один раз для каждого сервиса нужно либо
rem вписать свои токены в .env, либо пройти OAuth-вход в браузере.

setlocal
cd /d "%~dp0"

echo === AI Service System - установка зависимостей ===
echo.

for %%D in (
    connectors\discord
    connectors\drive
    connectors\gmail
    connectors\fibbee
    connectors\servicedesk
    connectors\tracker
    connectors\sheets_export
    core\router_ai
    core\webapp
) do (
    if exist "%%D\requirements.txt" (
        echo --- %%D ---
        if not exist "%%D\venv" (
            python -m venv "%%D\venv"
        )
        "%%D\venv\Scripts\pip.exe" install -q -r "%%D\requirements.txt"
        if exist "%%D\.env.example" if not exist "%%D\.env" (
            copy "%%D\.env.example" "%%D\.env" >nul
            echo    .env создан из .env.example — впиши свои токены/ключи, если ещё не сделал
        )
    )
)

rem Необязательный бандл готовых учётных данных для бета-тестеров — НЕ в
rem git (см. .gitignore), раздаётся отдельно самим владельцем проекта
rem (не через публичный репозиторий). Если папка beta_credentials лежит
rem рядом с этим файлом — раскладываем .env/credentials.json по нужным
rem местам поверх .env.example-заготовок.
if exist "beta_credentials\" (
    echo.
    echo Найден beta_credentials\ — раскладываю учётные данные...
    xcopy /s /y "beta_credentials\*" "." >nul
)

echo.
echo === Готово. Запускаю веб-морду (core\webapp) ===
start "" cmd /c core\webapp\supervisor.bat
timeout /t 5 /nobreak >nul
start "" http://127.0.0.1:9000

echo.
echo Веб-морда должна открыться в браузере на http://127.0.0.1:9000
echo Если не открылась — что-то не так с одним из шагов выше, см. вывод.
echo Раздел "Помощь" в самом приложении описывает, что делает каждый модуль
echo и что нужно для первого входа в каждый сервис.
pause
