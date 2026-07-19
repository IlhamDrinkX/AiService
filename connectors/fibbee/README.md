# Fibbee ERP connector

Read (полноценно) + write (реализовано, часть — намеренно не проверялась
живьём, см. ниже) через внутренний JSON API `erp.fibbee.com` — систему
мониторинга и управления кофейными комплексами/вендинговыми точками.

## Как это работает

1. Логин — обычный email+password (не SSO):

   ```
   POST /v1/auth/create
   {"email": "...", "password": "..."}
   -> {"success": true, "token": "<JWT>", "userId": ..., "role": "admin", ...}
   ```

2. Токен передаётся в заголовке (не `Authorization: Bearer`):

   ```
   x-auth-token: <JWT>
   x-lang: ru
   ```

3. `client.py` логинится сам (email/password из `.env`), кэширует токен в
   `FIBBEE_TOKEN_PATH`, логинится заново автоматически при 401/403.
4. `sync.py` тянет комплексы, остатки, заказы/тикеты/аудит-лог за окно
   `FIBBEE_SYNC_DAYS` дней (по умолчанию 2) и складывает всё в SQLite.

## Как искали API (метод discovery)

Как и для Tracker/Service Desk — открытого API и документации нет.
Использована техника из памяти [[feedback-endpoint-discovery-technique]]:
Desktop Commander (терминал на реальной машине) → Chrome перезапущен с
`--remote-debugging-port=9222` (для erp.fibbee.com — с отдельным свежим
`--user-data-dir`, т.к. свежие версии Chrome не разрешают remote-debugging
на дефолтном профиле пользователя из соображений безопасности) → Playwright
подключился по CDP.

Дальше — статический разбор фронтенд-бандла (`application.<hash>.js`, один
файл, не обфусцирован): весь API-клиент лежит в одном месте открытым
текстом в виде объектов `{action:"/orders/list", method:"get", user,
params} → No({...}) → axios на "/v1"+action`. Разбор дал полную карту из
**125 эндпоинтов** одним проходом (см. список ниже) — в разы быстрее, чем
перехватывать трафик по одной UI-операции за раз. Дальше каждый нужный
эндпоинт (orders/tickets/changes-log/incidents/healthchecks/...)
подтверждён живым запросом через `curl_cffi` с реальным токеном — включая
контрольные сравнения параметров (см. "Известные ограничения" ниже: часть
параметров, которые выглядели как рабочие фильтры по аналогии с другими
эндпоинтами, на деле игнорируются сервером).

## Комплексы, дашборды, заказы, тикеты, логи — что где

| Что нужно | Метод клиента | Эндпоинт |
|---|---|---|
| Список комплексов + статус (Нормал/Оффлайн/Обслуживание) | `get_sales_points()` | `GET /v1/sales-points/list` |
| Остатки по устройствам на точке | `get_sales_point_healthchecks()` | `GET /v1/sales-points/healthchecks` |
| Состояние подсистем комплекса (список "ОК"/не ОК по узлам) | `get_supervisor_config(sales_point_id)` | `GET /v1/supervisor/config` |
| История заказов (с фильтром по датам/точке/статусу) | `get_orders(...)` | `GET /v1/orders/list` |
| Один заказ целиком | `get_order(order_id)` | `GET /v1/orders/list?orderId=...` |
| **Лог заказа** (сырая телеметрия варки) | входит в каждый заказ, поле `productDump` | — |
| Тикеты (раздел "Сервис") | `get_tickets(...)` | `GET /v1/tickets/list` |
| Журнал инцидентов комплекса (с трассировкой до заказа/рецепта) | `get_incidents(sales_point_id)` | `GET /v1/incidents/list` |
| Общий аудит-лог изменений сущностей | `get_changes_log(...)` | `GET /v1/changes-log/list` |
| HTML-шелл живого дашборда (см. предупреждение ниже) | `get_dashboard_html(sales_point_id)` | `GET /dashboard/view/{id}/dashboard.html` |

### Заказы = история заказов И источник логов одновременно

Каждый объект заказа из `get_orders()`/`get_order()` содержит поле
**`productDump`** — сырые данные конкретной варки напитка (`nozzle`,
`milkTemp`, `waterQnty`, `cakePressFinal`, `drinkWeight`, `extractTime`,
`productsConsumption` и т.д.). Это и есть "лог заказа" для анализа —
отдельного эндпоинта под логи не требуется, он уже внутри истории заказов.
В SQLite это отдельная колонка `fibbee_orders.product_dump` (JSON), чтобы
можно было анализировать без парсинга всего `raw_json`.

### "Дашборд" — важное уточнение (ошибка в процессе discovery, исправлено)

На карточке каждого комплекса есть ссылка "Дашборд", которая открывает
`/dashboard/view/{salesPointId}/dashboard.html?token=<JWT>` — отдельное от
`/v1/` API мини-приложение с вкладками Dashboard/Orders/Minimap/Logs:
реальное состояние оборудования (лифты выдачи, диспенсер стаканов, кассовые
чеки, платёжные терминалы, манипуляторы), журнал передачи смены и историю
заказов с тем же Product Dump.

При первом перехвате трафика через Playwright показалось, что это
сервер-рендеренный HTML со всеми данными сразу (переключение вкладок не
делало новых `fetch`/`xhr`/`document`-запросов). При проверке через
`curl_cffi` без браузера выяснилось, что это неверно: страница — тонкий
SPA-шелл (`<div id="app">` + `db.js`), а реальные данные приходят по
**WebSocket** на `wsroot = "/dashboard/monitor/{salesPointId}"`, который не
попадает в перехват fetch/xhr/document (тот же класс проблемы, что и с
SSE-подключениями, см. [[feedback-endpoint-discovery-technique]] п.9, но
для WebSocket). Прямой GET без браузера возвращает ~360 байт шелла, без
единого заказа.

**Практический вывод:** для мониторинга и анализа не нужен WebSocket —
всё, что показывает эта страница (заказы+productDump, состояние точки),
уже доступно через чистые REST-эндпоинты (`get_orders`,
`get_sales_point_healthchecks`, `get_supervisor_config`). WebSocket дал бы
только *живую потоковую* телеметрию оборудования в реальном времени
(секундное обновление лифтов/манипуляторов) — отдельная, не реализованная
здесь задача, если когда-то понадобится именно потоковый мониторинг, а не
периодический снимок.

## Полный список найденных action-ов API (125 шт., статический разбор бандла)

Не все реализованы как методы клиента — только то, что нужно для
мониторинга/анализа плюс основные write-операции (см. "Про запись" ниже).
Полный список (для справки, если понадобится что-то ещё):

```
auth/create, badges/{create,delete,list,update}, campaigns/{create,list,update},
cashier-devices/{list,update}, cashier-notifications/{create,delete,list,update},
changes-log/list, coffee-bases/{create,delete,list,update},
device-models/list, devices/{create,delete,list,supply,update},
dictionaries/{list,update}, franchisees/{create,delete,list,update},
incidents/list, menu-categories/{create,delete,list,patch-menu-item-categories,update},
menu-items/{create,delete,list,restore,update}, messages-log/metrics,
milk-types/{list,update}, order-reviews/update, orders/{list,refund},
parcels/{create,delete,list,receive,ship,update},
payment-providers/{create,list,update}, permissions/list,
persistence/orderPaymentReceived, ping,
polls/{aggregated-votes,answers-list,create,delete,list,update,users-count},
products/{create,list,update}, promocodes/{create,generate-code,list,update,usages/list},
roles/{create,delete,list,update}, sales-point-devices/{list,update},
sales-points/{clone,delete,healthchecks,list,patch-menu-item-sales-points,terminals,update},
sales-report/{calculation-report,calculation-report-aggregated,cohorts-transitions,
  customers-metrics,downtime-report,ingredients-consumption-report(-aggregated),
  month-cohorts-report,non-production-consumption-report,orders-metrics,
  prime-costs-report,sales-predictions,sales-report,supply-changes,
  supply-predictions,supply-report,users-report},
supervisor/{config,device/{up,down},power-manager/{shutdown,startup}},
tickets/{create,list,update}, tweak/{create,delete,list,update}, uploader/list,
users/{create,fake-accumulated-bonus,list,patch-sales-points,update},
warehouses/{list,supply}
```

`sales-report/*` — раздел "Отчёты" в UI (продажи, себестоимость,
когорты, прогнозы поставок и т.п.) — не реализовано в клиенте, не входило
в задачу мониторинга, но эндпоинты найдены и задокументированы здесь на
будущее.

## Известные ограничения / грабли discovery

- **`orders/list` реально фильтрует по `startDate`/`endDate`** (ISO-строка
  вроде `"2026-07-18"`) — проверено контрольным сравнением: узкий диапазон
  дал `total=25` вместо `total=92975` без диапазона. Имена `dateFrom`/
  `dateTo`/`from`/`to`/`receivedAtFrom` сервер молча игнорирует (total не
  меняется) — только `startDate`/`endDate`.
- **`tickets/list` и `changes-log/list` НЕ фильтруют по дате вообще** —
  тот же тест (узкий/широкий/отсутствующий диапазон) дал идентичные
  результаты. Оба списка отсортированы по убыванию даты (`createdAt` /
  `updatedAt`), поэтому `sync.py` листает страницы и останавливается сам,
  как только видит запись старше нужного окна (плюс жёсткий потолок
  `MAX_PAGES_UNBOUNDED=200` страниц на случай сбоя сортировки).
- **`incidents/list` без `salesPointId` подвисает** (проверено — таймаут
  >20 сек без единого байта ответа). `client.get_incidents()` требует
  `sales_point_id` и бросает `ValueError`, если его не передать.
- `orders/list` без явного `limit` отдаёт 1000 записей (это дефолтный
  лимит страницы, не "все записи"). Фронтенд при массовом экспорте
  (Отчёты → Export productDump) листает `offset += 1000` до `offset <
  100000` — предполагаемый серверный кап, отдельно не подтверждали.
- Не путать два похожих, но разных механизма "дашборда": `getSalesPointToken`
  (`GET /dashboard/token/:salesPointId`, без `/v1`-префикса) — не
  исследовали отдельно; ссылка "Дашборд" в UI использует обычный
  логин-JWT как `?token=` к `/dashboard/view/...` напрямую, это и
  реализовано в `get_dashboard_html()`.

## Про запись (write)

Реализовано в `client.py`:

- `update_sales_point(data)` — `POST /v1/sales-points/update`.
- `create_ticket(data)` / `update_ticket(data)` — `POST /v1/tickets/{create,update}`.
- `update_order_review(data)` — `POST /v1/order-reviews/update`.
- `refund_order(data)` — `POST /v1/orders/refund` (реальный возврат денег).
- `supervisor_device_up(data)` / `supervisor_device_down(data)` — `POST /v1/supervisor/device/{up,down}`.
- `supervisor_startup(data)` / `supervisor_shutdown(data)` — `POST /v1/supervisor/power-manager/{startup,shutdown}`.

**Ни один write-метод не был вызван на боевых данных при разработке
коннектора** — в отличие от Service Desk/Tracker, где тестовые записи
безопасны (максимум остаётся мусорная заявка с пометкой "ТЕСТ"), здесь
последствия реальны:

- `tickets/create` — судя по полю `discordLink` в ответах `get_tickets()`
  и `slackUrl`-вебхуку в конфиге каждой точки, тикеты дублируются в
  Discord-канал поддержки. Тестовый тикет реально уведомит команду.
- `orders/refund` — настоящий возврат денег покупателю.
- `supervisor/device/{up,down}`, `power-manager/{startup,shutdown}` —
  прямое управление физическим оборудованием на боевой точке (кофемашина,
  лифты выдачи и т.п.).
- `sales-points/{update,delete,clone}` — структурные изменения боевого
  комплекса; `delete`/`clone` не реализованы в клиенте вовсе (не было
  нужды, и это самые опасные операции из всех).

Прежде чем звать любой из write-методов на реальном `salesPointId` —
подтвердить с пользователем. Названия полей для `data` в каждом методе
восстановлены только из формы ответов `list`-эндпоинтов (какие поля
сущность содержит), не из живого успешного write-запроса — поэтому точный
набор обязательных полей может отличаться, как это уже было с Service Desk
(`contractDate`/`contractUntil` там оказались обязательными не сразу).

## Отчёт по токам моторов и температуре ТЭН (`motor_ten_report.py`)

По запросу пользователя — диагностика "ОК / не ОК" по работе моторов
(насосы coffee/milk/water) и ТЭН/бойлера на основе уже синхронизированных
заказов, без обращения к API. Запуск: `python motor_ten_report.py` (после
`sync.py`) — кладёт xlsx в `./reports/` (не в git, см. `.gitignore`).

Ключевая находка при разработке: токи моторов лежат не на верхнем уровне
`productDump`, а во вложенном массиве `partResults[].avgCurrent` (плюс
сырые `pump_R_IS`/`pump_L_IS` внутри `tempLog` каждого элемента) — при
первом статическом обзоре ключей (только верхний уровень, без рекурсии)
это было пропущено. Формат встречается у фирменных комплексов DrinkX
("drinkx"-формат, 11 из 95 комплексов в первом прогоне) и покрывает на
порядок больше заказов, чем температура бойлера (`boilerTemp`/`waterTemp`
верхнего уровня, найдена только у комплексов со сторонней кофемашиной
"eversys"). Тока именно ТЭНа (в амперах) нигде в данных нет — статус ТЭН
оценивается только по стабильности/диапазону температуры бойлера.

Пороги "не ОК" (Z-отклонение тока >3σ от своей группы комплекс+модуль,
нулевой ток ≤0.02А, температура бойлера вне 85–115°C или отклонение >4°C
от среднего по комплексу) — статистическая эвристика по первому прогону
данных, не паспортные допуски производителя. Подробное обоснование и как
их пересматривать — в листе "Методика и ограничения" самого отчёта.

## Запуск

```bash
cd connectors/fibbee
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# вписать FIBBEE_PASSWORD в .env

python sync.py
```

`FIBBEE_SYNC_DAYS` (по умолчанию 2) — глубина окна для заказов/тикетов/
аудит-лога. Полный прогон на 2 дня по всем ~95 комплексам занял на боевом
прогоне около полутора минут: 2221 заказ, 3736 тикетов (в базе — включая
исторические, попавшие туда до правки клиентской пагинации, см.
`PROGRESS.md`), 16415 записей аудит-лога.

`python sync.py` — процесс не короткий (десятки секунд — пара минут в
зависимости от `FIBBEE_SYNC_DAYS`); если запускать через инструмент с
собственным таймаутом (например, MCP-обёртку терминала), лучше запускать в
фоне и опрашивать лог, а не ждать синхронно.

## Модель данных (SQLite)

- `fibbee_sales_points` — комплексы (как было: статус, локация, франчайзи,
  мойки, журнал передачи смены, `raw_json` с полным объектом).
- `fibbee_orders` — заказы, включая `product_dump` (лог заказа) отдельной
  колонкой.
- `fibbee_tickets` — тикеты "Сервис", включая `discord_link`.
- `fibbee_changes_log` — аудит-лог изменений.
- `fibbee_healthchecks` — снимок остатков по устройствам на точку (весь
  объект как JSON, ключи — id менюайтемов).

Везде, где форма вложенных полей не была стопроцентно ясна из одного
примера ответа, в колонки вытащено только то, в чём уверены — `raw_json`
хранит объект целиком, так что более глубокий разбор не потребует
повторного похода в API.
