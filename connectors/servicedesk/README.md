# DrinkX Service Desk connector

Read + write, через внутренний JSON API `sd.drinkx.tech` (не DOM-скрейпинг —
у SPA есть нормальные JSON-эндпоинты). Чтение найдено через DevTools Network,
запись (2026-07-19) — перехватом трафика реального Chrome пользователя через
CDP/Playwright, с живым тестированием на выделенном тестовом клиенте/объекте
(`цццццц` · Day2Day · 5-ка Бауманская). Подробности процесса — в PROGRESS.md.

## Как это работает

1. У Service Desk нет отдельного API-токена — вход только через Google SSO
   (домен `drinkx.tech`), и сессия держится на обычных cookie.
2. Cookie нужно получить из уже рабочей залогиненной вкладки — см.
   "Получение cookie" ниже. Способ через отдельный автоматизированный
   браузер (`login.py`, Playwright) **не работает**: сайт недоступен из
   свежего Chrome-профиля — похоже, доступ завязан на VPN-расширение/
   корп-политику/сертификат, привязанные к конкретному профилю браузера,
   которых у чистого профиля просто нет (ловили `net::ERR_ABORTED` /
   `net::ERR_TIMED_OUT` при попытке открыть сайт в отдельном окне, хотя в
   обычном рабочем профиле всё грузится нормально). `login.py` оставлен в
   репозитории на случай, если на другой машине без такого ограничения он
   заработает, но основной путь — `import_cookie.py`.
3. `client.py` переиспользует cookies через `curl_cffi` (не браузер, без
   токенов в заголовках — один cookie `sd_session`). Обычный `httpx`/`requests`
   тут не работает: `sd.drinkx.tech`, как и трекер, режет TLS-хендшейк для
   нестандартных клиентов — соединение зависает намертво на этапе TLS,
   а не отдаёт понятную ошибку. `curl_cffi` с `impersonate="chrome"`
   воспроизводит браузерный TLS-отпечаток и проходит эту проверку.
4. `sync.py` тянет `/api/prototype/tickets` и складывает в SQLite (только чтение).
5. Запись идёт напрямую через `client.py`, тем же `curl_cffi`-клиентом, без
   браузера — заявки (`create_ticket`/`update_ticket`/`set_status`/...),
   клиенты, объекты, комплексы, сотрудники, проблемы, справочники. См.
   раздел "Про запись" ниже.

## Получение cookie (основной способ)

```bash
cd connectors/servicedesk
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

python import_cookie.py
python sync.py
```

Что вставлять в `import_cookie.py`, когда он спросит:

1. Открой `sd.drinkx.tech` в браузере, где он уже нормально работает
   (залогинен, видишь список заявок).
2. F12 → вкладка **Network** → обнови страницу (F5).
3. Кликни на любой запрос к `sd.drinkx.tech` (например `tickets` или
   `prototype/tickets`).
4. Справа → **Headers** → **Request Headers** → найди строку `cookie:` →
   скопируй значение целиком (одна длинная строка вида
   `name1=value1; name2=value2; ...`).
5. Вставь эту строку в `import_cookie.py`, когда он попросит.

## Когда обновлять cookie

Если `sync.py`/`client.py` падают с `ServiceDeskAuthError` ("сессия истекла
или отозвана" / "ответ не JSON, похоже увели на страницу логина") —
повтори шаги выше и перезапусти `import_cookie.py`. Точный срок жизни
сессии не известен, скрипт сам об этом сообщит по факту ошибки.

## Известные эндпоинты

Чтение:

| Эндпоинт | Метод |
|---|---|
| `/api/prototype/tickets` | GET — полный список заявок, используется в `sync.py` |
| `/api/auth/me` | GET — текущий пользователь (`client.get_auth_status()`) |
| `/api/clients` | GET — список клиентов (`client.get_clients()`) |
| `/api/prototype/objects` | GET — объекты/точки (`client.get_objects()`) |
| `/api/prototype/complexes` | GET — комплексы оборудования (`client.get_complexes()`) |
| `/api/prototype/employees` | GET — сотрудники (`client.get_employees()`) |
| `/api/prototype/problems` | GET — справочник проблем (`client.get_problems()`) |
| `/api/prototype/dictionary-groups` | GET — прочие справочники (`client.get_dictionary_groups()`) |

Запись — два разных паттерна (перепроверено на практике для каждой сущности
отдельно, 2026-07-19, см. также PROGRESS.md):

| Эндпоинт | Метод | Паттерн |
|---|---|---|
| `/api/prototype/tickets/{id}` | PUT, body `{"payload": {...}}` | prototype, id генерирует клиент |
| `/api/prototype/objects/{id}` | PUT, body `{"payload": {...}}` | prototype |
| `/api/prototype/complexes/{id}` | PUT, body `{"payload": {...}}` | prototype |
| `/api/prototype/employees/{id}` | PUT, body `{"payload": {...}}` | prototype |
| `/api/prototype/problems/{id}` | PUT, body `{"payload": {...}}` | prototype |
| `/api/prototype/dictionary-groups/{group_id}` | PUT, body `{"payload": {...вся группа с items[]...}}` | prototype, id — id ГРУППЫ, не элемента |
| `/api/clients` | POST, body `{...без id...}` | обычный REST, сервер сам назначает id |
| `/api/clients/{id}` | PUT, body `{...полный объект...}` | обычный REST |

"prototype"-паттерн: PUT — full-replace, id заявки/объекта/... генерирует
клиент (`_gen_id`), сервер не проверяет формат и не различает создание от
обновления (если id ещё не было — считай, что создал; если был — обновил).
"clients" — единственное исключение, обычный REST без обёртки `payload`.

`GET /api/prototype/tickets/{id}` (чтение одной записи) **не существует ни
для одной prototype-сущности** — проверено на tickets, отдаёт чистый
`404 {"message":"Cannot GET ..."}`. Поэтому перед любым обновлением нужен
полный список (`get_tickets()`/`get_objects()`/...), чтобы найти текущее
состояние — `client.py` делает это сам, если явно не передать `base_*`.

**Осторожно с очень быстрыми последовательными PUT к одной и той же
записи справочника** (`dictionary-groups`) — на практике поймали race: два
PUT подряд (update текста, затем деактивация) с интервалом в десятки
миллисекунд иногда приводят к тому, что промежуточное состояние теряется
(ответ на первый PUT показывает уже "будущее" значение поля, которое должен
был выставить только второй). Похоже на гонку в самом прототип-бэкенде
(read-modify-write без блокировки), не в клиенте. Если пишешь несколько
изменений подряд в одну и ту же запись — лучше дождаться ответа и, если
критично, перепроверить состояние отдельным чтением, чем слать пачкой без
пауз.

## Модель данных заявки

Каждая заявка (`servicedesk_tickets`) содержит: код (`SD-1025`), тип
оборудования (`node`), клиента, объект, статус, приоритет, критичность
(`severity`), инженера, диагностику, результат работ и т.д. — полный список
полей см. в `storage.py::SCHEMA`.

Вложенные структуры хранятся в отдельных таблицах:
- `servicedesk_ticket_audit` — история статусов/событий заявки.
- `servicedesk_ticket_files` — метаданные вложений; сам файл (фото акта,
  PDF) декодируется из `dataUrl` (base64 в JSON) и кладётся на диск в
  `FILES_DIR`, в базе только путь. Без этого SQLite быстро раздулся бы —
  вложения многомегабайтные.

## Про запись (создание/обновление заявок)

Реализовано (2026-07-19). Ключевой факт про API, без которого легко всё
сломать: **PUT — это full-replace, не patch**. Сервер не мержит частичные
поля — ждёт весь объект заявки (все ключи, что отдаёт `get_tickets()`,
включая `files[].dataUrl` и весь `audit[]`). Сама SPA перед каждым
изменением берёт текущее состояние заявки, применяет изменение и дописывает
в `audit` новую запись `{actor, at, event, diff}` — историю ведёт клиент, не
сервер. `client.py` воспроизводит это же поведение.

Ещё один факт, который стоил одного "битого" тестового тикета: **код
`SD-1027` присваивает не сервер, а фронтенд** — сервер просто хранит то, что
получил в `payload.code`. `create_ticket()` сам вычисляет следующий
свободный `SD-xxxx` (максимум существующих + 1) через `_next_ticket_code()`,
если не передать `code` явно. При параллельном создании двух заявок в один
момент возможна коллизия номеров — это ограничение самой системы (в реальном
UI при одновременной работе двух людей та же гонка тоже есть), не наше.

### Методы `ServiceDeskClient`

```python
client.create_ticket(
    complex_name="4.11", object_name="Покровка 10с1", client_name="Вектор",
    node="Кофемашина", title="...", description="...",
    severity="Функционал не ограничен", priority="Средний", actor="AI-агент",
)  # -> заведёт новую заявку L1, вернёт полный объект с присвоенным code

client.set_status(ticket_id, "Новая L2")
client.assign_engineer(ticket_id, "Иванов Иван")
client.add_l1_action(ticket_id, "Перезагрузили удалённо")
client.add_diagnosis(ticket_id, "Течь из соединения БРС")
client.add_engineer_comment(ticket_id, "Заменили клапан, акт подписан")
client.upload_file(ticket_id, "/path/to/photo.jpg")  # фото акта/диагностики

# низкоуровневый доступ, если нужен полный контроль над payload:
client.update_ticket(ticket_id, {"priority": "Высокий"}, event="Приоритет повышен")
client.put_ticket(ticket_id, full_payload)
```

Все update-методы по умолчанию сами делают `get_tickets()`, чтобы взять
свежее состояние перед PUT (иначе рискуешь затереть чужие изменения полей,
которые не трогал). Если уже есть свежий объект заявки под рукой (например,
только что вернул `create_ticket`/предыдущий вызов) — передай его через
`base_ticket=...`, чтобы не делать лишний запрос списка.

Отдельного "треда комментариев" в этой модели данных нет — ближайший аналог
это поле `engineerComment` (см. вкладку "Работы и акт" в UI). Загрузка файла
не имеет отдельного upload-эндпоинта: вложение кладётся base64-строкой прямо
в `files[].dataUrl` того же PUT-запроса.

### Клиенты, объекты, комплексы, сотрудники, проблемы, справочники

Реализовано (2026-07-19, второй заход после базовой записи по заявкам).

```python
# Клиенты (обычный REST, сервер сам назначает id)
cl = client.create_client(brand_name="Вектор", legal_name='ООО "Вектор"',
                           inn="7700000000", contract_number="123")
client.update_client(cl["id"], {"comment": "новый контакт: ..."})

# Объекты/точки
obj = client.create_object(client_name="Вектор", name="Сибур", address="ул. ...")
client.update_object(obj["id"], {"accessRules": "свободный доступ"})

# Комплексы оборудования
cx = client.create_complex(serial="4.20", client_name="Вектор", object_name="Сибур", version="v4")
client.update_complex(cx["id"], {"waterSupply": "центральное"})

# Сотрудники (role: "observer" подтверждён на практике для "Наблюдатель";
# коды для "Инженер"/"Координатор" не проверяли — смотри реальный PUT в
# DevTools при редактировании такого сотрудника, если понадобится)
emp = client.create_employee(last_name="Иванов", first_name="Иван", email="i.ivanov@drinkx.tech",
                              role="observer", schedule="2/2, 09:00-21:00")  # role="engineer" — угадано, не подтверждено PUT'ом
client.update_employee(emp["id"], {"presenceStatus": "неактивен"})

# Проблемы (корневые причины) + привязка к заявке
pr = client.create_problem(title="Котята прибиваются", description="...")
client.update_problem(pr["id"], {"description": "новое описание"})
client.link_problem(ticket_id, pr["id"])       # привязать (Работы и акт -> Корневая проблема -> Выбрать)
client.link_problem(ticket_id, None)           # отвязать (кнопка "Отвязать" в UI)

# Справочники (work-types / failure-reasons / nodes) — операции на уровне
# ГРУППЫ, добавление одного значения = PUT всей группы с новым items[]
client.add_dictionary_item("work-types", name="Диагностика удалённо", description="...")
client.update_dictionary_item("work-types", item_id, {"description": "..."})
client.deactivate_dictionary_item("work-types", item_id)  # мягкое удаление (active=False)

# Заявки за диапазон дат / весь список
all_tickets = client.get_tickets()  # весь список, как и раньше
july = client.get_tickets_in_range("2026-07-01", "2026-07-31")            # по incidentAt
created_in_range = client.get_tickets_in_range("2026-07-01", None, date_field="createdAt")
```

`create_object`/`create_complex`/`create_employee`/`create_problem` следуют
тому же "prototype"-паттерну, что и `create_ticket` (id генерирует клиент,
PUT = full-replace), но БЕЗ audit-лога и без вычисления человекочитаемого
кода — это специфика только тикетов. `create_client`/`update_client` — на
обычном REST, без обёртки `payload`; сервер валидирует `contractDate`/
`contractUntil` как обязательные ISO-даты (пустая строка -> `400 Bad
Request`) — если не передать явно, `create_client` подставляет сегодня и
+365 дней как безобидный дефолт.

### Тестовые данные, оставшиеся в проде

**Discovery делался на реальных данных**, но на выделенных тестовых
записях, которые либо указал пользователь, либо завели сами — все с пометкой
"ТЕСТ"/"discovery" в названии, можно удалить или проигнорировать:
- Заявки: одна без кода (до фикса `_next_ticket_code`), SD-1027–SD-1030.
- Клиент "ТЕСТ-клиент discovery" / "ТЕСТ-клиент curl_cffi" (+ их объекты,
  комплексы на объекте `цццццц` · Day2Day · 5-ка Бауманская).
- Сотрудник "ТЕСТ-сотрудник Discovery" / "ТЕСТ-curl Cffi" (роль
  "Наблюдатель", статус "неактивен" — не должен попадать в назначения).
- Проблемы "ТЕСТ-проблема discovery" / "ТЕСТ-проблема curl_cffi".
- Значение справочника "Виды работ" → "ТЕСТ-вид работ discovery" /
  "ТЕСТ-вид работ curl_cffi" (последнее деактивировано, `active=false`).

Удалить через API нельзя — DELETE-эндпоинтов не нашли ни для одной сущности
(и не искали, не входило в задачу). Для справочников есть мягкое удаление
(`deactivate_dictionary_item` / чекбокс "Активно" в UI) — им и
воспользовались вместо реального удаления.

## Про клиент: почему curl_cffi, а не httpx/requests

Изначально ставился обычный `httpx` (казалось, что здесь нет отдельного WAF —
разовый `net::ERR_TIMED_OUT` при ручной проверке из консоли браузера выглядел
как случайная флуктуация). На практике `sync.py` зависал намертво на
TLS-хендшейке (`do_handshake()`, снимается только Ctrl+C) — то есть защита
такая же, как у трекера: режет TLS-отпечаток нестандартных клиентов, а не
блокирует по HTTP-уровню. Ручная проверка через `fetch()` в консоли браузера
этого не показывала, потому что шла с TLS-отпечатком настоящего Chrome.
`curl_cffi` с `impersonate="chrome"` (см. `client.py`) воспроизводит этот
отпечаток и проходит проверку — как и в `connectors/tracker/client.py`.
