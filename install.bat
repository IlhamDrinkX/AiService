@echo off
rem Устанавливает проект с GitHub с нуля на новую машину (бета-тестер).
rem Использование: скачай/скопируй этот файл на новую машину и запусти
rem его ДВОЙНЫМ КЛИКОМ в пустой папке, куда хочешь установить проект.
rem
rem ВАЖНО перед раздачей бета-тестерам: пропиши ниже REPO_URL — адрес твоего
rem GitHub-репозитория (см. README.md/BETA_DISTRIBUTION.md, там же — почему
rem личные API-ключи НЕ зашиваются в этот скрипт и не лежат в репозитории).

setlocal
set REPO_URL=https://github.com/IlhamDrinkX/AiService.git
set REPO_BRANCH=main
set INSTALL_DIR=%~dp0AI_service_system

echo === AI Service System - установка ===
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo Git не найден. Пробую поставить через winget...
    winget install --id Git.Git -e --source winget
    if errorlevel 1 (
        echo.
        echo Не получилось поставить Git автоматически.
        echo Поставь вручную: https://git-scm.com/downloads
        echo и запусти этот файл ещё раз.
        pause
        exit /b 1
    )
    echo Git поставлен. Возможно, потребуется перезапустить этот файл
    echo в новом окне терминала, если команда git всё ещё не находится.
)

if exist "%INSTALL_DIR%\.git" (
    echo Репозиторий уже есть, обновляю до последней версии %REPO_BRANCH%...
    cd /d "%INSTALL_DIR%"
    git fetch origin
    git checkout %REPO_BRANCH%
    git pull origin %REPO_BRANCH%
) else (
    echo Клонирую %REPO_URL% (%REPO_BRANCH%)...
    git clone --branch %REPO_BRANCH% "%REPO_URL%" "%INSTALL_DIR%"
)

if errorlevel 1 (
    echo.
    echo Клонирование/обновление не удалось — проверь REPO_URL в этом файле
    echo и доступ к репозиторию (если он приватный, нужен вход в GitHub).
    pause
    exit /b 1
)

echo.
echo Код на месте: %INSTALL_DIR%
echo Дальше — запусти setup_all.bat из этой же папки, он поставит
echo зависимости и подскажет, что нужно для авторизации.
pause
