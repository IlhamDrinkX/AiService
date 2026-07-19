# DrinkX Tracker connector

Read + write, через внутренний JSON API `tracker.drinkx.tech` (SPA-эндпоинты,
не скрейпинг). До 2026-07-19 был только официальный Bearer-токен API с
scope `tasks:read` — только чтение, без привязки к конкретному человеку.
Переделано по аналогии с `connectors/servicedesk`: полноценный read+write
через cookie-сессию того же типа, что использует сама SPA после Google SSO.
Подробности процесса — в PROGRESS.md за 2026-07-19.

## Как это работает

1. У трекера нет отдельного write-API-токена — вход через Google SSO (домен
   `drinkx.tech`), сессия держится на одном httpOnly cookie `sid`.
2. Cookie получается из уже залогиненной вкладки — см. "Получение cookie"
   ниже (`import_cookie.py`, тот же процесс, что и для Service Desk).
3. `client.py` использует cookie через `curl_cffi` (обычный `httpx`/`requests`
   не проходит — TLS-хендшейк режется нестандартным клиентам, см. раздел
   про curl_cffi в конце).
4. Все действия идут от имени залогиненного пользователя: `reporter`/`author`
   в ответах API — реальный человек (Ilham Khabibulin), не сервисный токен.
   Это и даёт "работать от своего имени" — задачи, комментарии, уведомления
   привязаны к настоящему аккаунту.
5. `sync.py` тянет `/api/task-types` + `/api/tasks?type=<CODE>` по всем
   доскам и складывает в SQLite (то же самое, что раньше, но через новый
   клиент).

## Получение cookie

```bash
cd connectors/tracker
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

python import_cookie.py
python sync.py
```

Что вставлять в `import_cookie.py`, когда он спросит:

1. Открой `tracker.drinkx.tech` в браузере, где ты уже залогинен.
2. F12 → вкладка **Network** → обнови страницу (F5).
3. Кликни на любой запрос к `tracker.drinkx.tech` (например `tasks` или `me`).
4. Справа → **Headers** → **Request Headers** → найди строку `cookie:` →
   скопируй значение целиком (нужен как минимум `sid=...`).
5. Вставь эту строку в `import_cookie.py`, когда он попросит.

## Когда обновлять cookie

Если `sync.py`/`client.py`/`healthcheck.py` падают с `TrackerAuthError`
("cookie истекла или отозвана" / "ответ не JSON, похоже увели на страницу
логина") — повтори шаги выше и перезапусти `import_cookie.py`. Точный срок
жизни сессии не известен, скрипт сам сообщит по факту ошибки. Также сессия
разово сбрасывается, если пользователь перелогинивается в трекере в браузере
(старый `sid` становится невалиден).

## Старый Bearer-токен (`tasks:read`)

Больше не используется этим коннектором — `client.py`/`sync.py` теперь
полностью на cookie-сессии. Токен со страницы "Интеграции" (если он ещё
существует) можно оставить как есть или отозвать — на работу коннектора это
не влияет. `.env.example`/`.env` больше не содержат `TRACKER_TOKEN`.

## Известные эндпоинты

Профиль/команда:

| Эндпоинт | Метод |
|---|---|
| `/api/me` | GET — текущий пользователь |
| `/api/team` | GET — список сотрудников (для назначения задач) |
| `/api/tags` | GET — все теги, встречавшиеся в задачах |
| `/api/focus` | GET — "Мой фокус" (задачи текущего пользователя) |

Доски/задачи:

| Эндпоинт | Метод |
|---|---|
| `/api/task-types` | GET — доски (DEV/TEST/CAD/ADM/OPS/LGL/PRC/FIN/PRJ/MFG/SLS) + статусы |
| `/api/tasks?type=<CODE>` | GET — все задачи доски |
| `/api/tasks/{id}` | GET — задача по uuid |
| `/api/tasks/by-code/{code}` | GET — задача по коду (`DEV-1234`) |
| `/api/tasks` | POST — создать задачу |
| `/api/tasks/{id}` | PATCH — обновить задачу, **частичный patch** |
| `/api/tasks/{id}[?cascade=true]` | DELETE — удалить задачу |
| `/api/tasks/{id}/subtasks` | GET |
| `/api/tasks/{id}/commits` | GET — привязанные git-коммиты |

Комментарии и вложения:

| Эндпоинт | Метод |
|---|---|
| `/api/tasks/{id}/comments` | GET, POST `{"body": "..."}` |
| `/api/comments/{id}` | PATCH `{"body": "..."}`, DELETE |
| `/api/tasks/{id}/attachments` | GET, POST multipart (поле `file`) |
| `/api/attachments/{id}` | DELETE |

Уведомления:

| Эндпоинт | Метод |
|---|---|
| `/api/notifications/summary` | GET — `{unreadCount, tasks}` |
| `/api/notifications[?status=unread]` | GET |
| `/api/notifications/read` | POST `{"ids": [...]}` или `{"commentId": "..."}` |

Реалтайм (SSE, не JSON — не оборачивается этим клиентом): `/api/task-events`.

Найдены, но не реализованы в клиенте (нет практической нужды — можно
добавить по тому же паттерну): `/api/releases*`,
`/api/admin/task-types/*/statuses*`, `/api/access-requests*`,
`/api/invitations*`, `/api/me/integrations/tokens*`.

### Ключевые факты про API

- **PATCH `/api/tasks/{id}` — настоящий частичный patch**, не full-replace
  как в Service Desk (`/api/prototype/*`). Можно прислать `{"statusId": "..."}`
  и только один это поле поменяется.
- **POST `/api/tasks` сам генерирует id и код** (`DEV-1234`) на сервере — в
  отличие от Service Desk, клиенту не нужно вычислять следующий номер.
- **DELETE полноценно работает** (в Service Desk вообще нет DELETE) —
  подтверждено на тестовых задачах, отвечает `{"ok": true}`.
- `assigneeId`/`parentTaskId` принимают `null` для снятия
  назначения/отвязки от родителя.
- `estimate` — объект `{"value": <число>, "unit": "hours"|"days"}`, не
  голое число.
- `description` — HTML-строка (Tiptap/ProseMirror-редактор на фронте).
- Все write-эндпоинты найдены перехватом трафика реального Chrome через
  CDP/Playwright **плюс** статическим разбором фронтенд-бандла
  (`/assets/index-*.js` — весь API-клиент SPA лежит там открытым текстом в
  одном месте, это оказалось быстрее и надёжнее, чем угадывать по одной
  UI-операции за раз).

### Методы `TrackerClient`

```python
from client import TrackerClient

c = TrackerClient("https://tracker.drinkx.tech", state_path="./tracker_state.json")

me = c.get_me()
boards = c.get_task_types()                 # доски + статусы
tasks = c.get_tasks("DEV")                   # все задачи доски

task = c.create_task("DEV", title="Заголовок", description="<p>Детали</p>")
c.set_status_by_name(task["id"], "DEV", "Готова к спринту")
c.assign(task["id"], someone_id)             # None — снять назначение
c.set_urgent(task["id"], True)
c.set_estimate(task["id"], 2, "hours")
c.set_tags(task["id"], ["backend", "bug"])

c.add_comment(task["id"], "Комментарий")
c.get_comments(task["id"])
c.upload_attachment(task["id"], "/path/to/file.png")

c.get_notifications_summary()                # {unreadCount, tasks}
c.mark_all_notifications_read()

c.delete_task(task["id"])                    # DELETE, полноценно работает
```

Полный список методов и докстринг с деталями пути discovery — в `client.py`.

## Тестовые данные

Discovery делался на реальных досках прода, но на тестовых задачах с
пометкой "ТЕСТ-discovery" в названии — все они удалены через `DELETE`
сразу после проверки (в отличие от Service Desk, где DELETE не было и
тестовые записи остались). В проде тестовых следов не осталось.

## Ограничения этой версии

- `sync.py` по-прежнему делает полный пересбор списка задач на каждый
  запуск (без инкрементальности) — быстро для объёма в пределах пары тысяч
  задач на доску.
- Realtime (`/api/task-events`, SSE) не используется — polling через
  `get_notifications_summary()`/`get_tasks()` вместо подписки на события.
- `/api/releases*`, `/api/admin/*`, `/api/access-requests*`,
  `/api/invitations*` найдены в бандле, но не обёрнуты методами клиента.

## Про клиент: почему curl_cffi, а не httpx/requests

`tracker.drinkx.tech`, как и `sd.drinkx.tech`, режет TLS-хендшейк для
нестандартных клиентов (`requests`/`httpx` виснут намертво на
`do_handshake()`, а не отдают понятную ошибку) — похоже на фильтрацию по
TLS-отпечатку (JA3/JA4). `curl_cffi` с `impersonate="chrome"` воспроизводит
браузерный TLS-отпечаток и проходит эту проверку. См. также
`connectors/servicedesk/client.py` — тот же фикс.
