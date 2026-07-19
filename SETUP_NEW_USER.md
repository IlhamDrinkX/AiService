# Настройка под свой аккаунт

Токены, `credentials.json`, `.env` и локальные базы (`data/`) не хранятся в git (см. `.gitignore`) — намеренно, чтобы разные люди не путали чужие доступы и корпоративные данные не утекали в репозиторий. Если тебе досталась копия этого проекта (клон, скачанный архив, форк), для запуска у себя нужно завести свои учётные данные — ничего не наследуется от исходного автора.

## Discord

1. discord.com/developers/applications → **New Application**.
2. Bot → **Reset Token** → скопируй токен.
3. Bot → Privileged Gateway Intents → включи **Message Content Intent**.
4. OAuth2 → URL Generator → scope `bot`, permissions **View Channels** + **Read Message History** → перейди по сгенерированной ссылке и добавь бота на свой сервер.
5. В `connectors/discord/` скопируй `.env.example` → `.env`, вставь `DISCORD_BOT_TOKEN`.

## Gmail и Google Drive

Оба коннектора могут использовать один и тот же OAuth-клиент Google — просто разные scopes и отдельные токены.

1. console.cloud.google.com → создай свой проект.
2. APIs & Services → Library → включи **Gmail API** и **Google Drive API**.
3. Google Auth Platform → Branding (заполнить) → Audience (External, добавить свою почту в Test users) → Data Access (добавить scopes `gmail.readonly` и `drive.readonly`).
4. Clients → Create OAuth client → тип **Desktop app**. Secret показывается один раз — сразу скопируй, либо потом жми "+ Add secret" для нового.
5. Собери свой `credentials.json` (см. шаблон в README каждого коннектора) и положи его в `connectors/gmail/` и `connectors/drive/` (можно один и тот же файл в обе папки).
6. В каждой из этих папок скопируй `.env.example` → `.env`.
7. Первый запуск (`python sync.py`) откроет браузер — войти под своим аккаунтом, разрешить read-only доступ. Токен сохранится локально в `data/token.json`, повторный вход не требуется, пока доступ не отозван вручную в Google Account → Security → Third-party access.

## OpenRouter (Router AI)

1. openrouter.ai → зарегистрируйся, пополни кредиты (settings/credits).
2. Settings → Keys → создай ключ.
3. В `core/router_ai/` скопируй `.env.example` → `.env`, вставь `OPENROUTER_API_KEY`.
4. Проверка: `core\router_ai\venv\Scripts\python core\router_ai\healthcheck.py`
   (после установки зависимостей — см. `core/router_ai/README.md`).

## Запуск веб-морды и автозапуск служб

Веб-интерфейс (`core/webapp/`), Discord-бот (`connectors/discord/`) и
планировщик синхронизаций (`core/scheduler/`) можно запускать вручную (через
их `.bat`/`start_webapp.bat` в корне) или настроить, чтобы они сами
поднимались при входе в Windows и сами перезапускались при падении.

Обычный способ (Планировщик заданий Windows, `schtasks`) требует прав
администратора — на многих рабочих машинах их нет, и `schtasks /create`
вернёт "Отказано в доступе". Поэтому используется способ без повышенных
прав:

1. В каждой из трёх папок (`core/webapp/`, `core/scheduler/`,
   `connectors/discord/`) уже есть `supervisor.bat` — это просто
   restart-loop: запускает нужный процесс, и если он упадёт, тут же
   перезапускает, без ручного вмешательства. Логи — в `logs/*_supervisor.log`,
   `logs/*_out.log`, `logs/*_err.log`.
2. Чтобы всё это поднималось само при входе в систему, положи в папку
   автозагрузки Windows (`Win+R` → `shell:startup`, обычно
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`) три `.vbs`-файла
   — по одному на каждую службу, — каждый одной строкой (подставь свой путь
   до проекта):
   ```vbs
   CreateObject("WScript.Shell").Run """C:\путь\до\проекта\core\webapp\supervisor.bat""", 0, False
   ```
   Аргумент `0` — окно скрыто (не мешает работать), `False` — не ждёт
   завершения (не блокирует вход в систему). Так же для
   `core\scheduler\supervisor.bat` и `connectors\discord\supervisor.bat`.
3. Планировщик синхронизаций (`core/scheduler/run.py`) сам решает, когда
   что синхронизировать — не нужно ничего дополнительно настраивать в
   Task Scheduler: Tracker/Service Desk/Fibbee — каждые 2 часа, Drive/Gmail —
   раз в сутки (ночью в 03:xx либо раньше, если компьютер простаивает ≥15
   минут). Требует Windows (использует WinAPI для определения простоя).
4. Проверить, что всё поднялось: `logs/webapp_supervisor.log`,
   `logs/scheduler.log`, `logs/discord_supervisor.log`, либо просто открыть
   `http://127.0.0.1:9000` в браузере.

## Важно

- Каждый, кто запускает свою копию проекта, создаёт **свой** Discord-бот и **свой** Google OAuth client. Общий `credentials.json`/токен — это прямой доступ к чужой личной почте и Drive, делиться им нельзя.
- Перед любым `git add` проверяй, что не добавляешь `.env`, `credentials.json`, `token.json` или что-то из `data/` — `.gitignore` их исключает по умолчанию, но при переименовании/копировании файлов легко случайно обойти правило.
- Если случайно закоммитил секрет — ротация токена/пересоздание OAuth client обязательна, `git rm` из истории не отменяет факт утечки.
