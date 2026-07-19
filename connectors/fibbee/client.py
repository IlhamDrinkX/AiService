"""
Клиент для Fibbee ERP (erp.fibbee.com) — системы мониторинга и управления
кофейными комплексами/вендинговыми точками.

Авторизация — email+password, не SSO:

    POST /v1/auth/create   {"email": ..., "password": ...}
    -> {"success": true, "token": "<JWT>", "userId": ..., "role": ..., ...}

Токен передаётся в заголовке (подтверждено и через DevTools, и статическим
разбором фронтенд-бандла):

    x-auth-token: <JWT>
    x-lang: ru

JWT из /v1/auth/create действует ~30 дней. Кэшируем на диске
(FIBBEE_TOKEN_PATH); при 401/403 логинимся заново автоматически.

Все эндпоинты ниже найдены статическим разбором фронтенд-бандла
(/application.<hash>.js — единственный бандл, весь API-клиент лежит в одном
месте открытым текстом: объект вида `{action:"/orders/list", method:"get",
user, params}` → `No({action, method, data, params, user})` → axios-запрос на
`"/v1" + action`) и подтверждены живыми запросами через curl_cffi. Приём —
как в connectors/tracker (см. память feedback-endpoint-discovery-technique):
статический разбор бандла в разы быстрее перехвата трафика по одной
операции за раз.

Базовый паттерн ответа: `{"success": true, ...}` или `{"success": false, ...}`
(тогда бросаем исключение). GET-эндпоинты обычно принимают params напрямую
как query-string (не обёрнуты, в отличие от Service Desk prototype-паттерна).

Полный список найденных в бандле action-ов (125 штук, не все реализованы
как методы клиента) — см. README.md. Ниже — только те, что нужны для
мониторинга (комплексы/дашборды/заказы/тикеты/логи) плюс основные write-
операции.

ВАЖНО про запись: часть write-эндпоинтов управляет реальным физическим
оборудованием на боевых точках (supervisor/device/up|down, power-manager
startup|shutdown, orders/refund — реальный возврат денег) или создаёт
тикеты, которые дублируются в Discord (`discordLink` в ответе tickets/list,
`slackUrl`-вебхук в конфиге точки) — то есть создание тестового тикета
реально уведомит команду поддержки. Эти методы реализованы (доступны), но
**не были прогнаны на боевых данных** при разработке коннектора — прежде
чем вызывать supervisor_*/refund_order/create_ticket на реальном
salesPointId, подтверди с пользователем.
"""

import json
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests

TIMEOUT_SECONDS = 30
IMPERSONATE = "chrome"


class FibbeeAuthError(Exception):
    """Логин не прошёл (неверный пароль/email) или сервер стабильно отвечает 401/403 после релогина."""


class FibbeeClient:
    def __init__(self, base_url: str, email: str, password: str, token_path: str):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token_path = Path(token_path)
        self.session = cffi_requests.Session(impersonate=IMPERSONATE)
        self.token = self._load_token()
        if not self.token:
            self.token = self._login()

    # ---- авторизация -----------------------------------------------------

    def _load_token(self) -> str | None:
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            return data.get("token")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_token(self, token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps({"token": token}, ensure_ascii=False), encoding="utf-8")

    def _login(self) -> str:
        resp = self.session.post(
            f"{self.base_url}/v1/auth/create",
            json={"email": self.email, "password": self.password},
            timeout=TIMEOUT_SECONDS,
        )
        try:
            data = resp.json()
        except ValueError:
            raise FibbeeAuthError(f"Логин не удался: сервер ответил не JSON ({resp.status_code})")

        if not data.get("success") or not data.get("token"):
            raise FibbeeAuthError(f"Логин не удался: {data}")

        self._save_token(data["token"])
        return data["token"]

    def _headers(self) -> dict[str, str]:
        return {"x-auth-token": self.token, "x-lang": "ru"}

    # ---- низкоуровневые запросы --------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None,
                 data: dict | None = None, retries: int = 3, raw: bool = False):
        """raw=True — вернуть текст ответа как есть (для /dashboard/view/.../dashboard.html,
        это не JSON API, а серверный рендер HTML)."""
        last_exc = None
        relogged_in = False

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=data if data is not None else None,
                    headers=self._headers(),
                    timeout=TIMEOUT_SECONDS,
                )
            except cffi_requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(3 * attempt)
                continue

            if resp.status_code in (401, 403) and not relogged_in:
                self.token = self._login()
                relogged_in = True
                continue

            if resp.status_code in (401, 403):
                raise FibbeeAuthError(
                    f"{resp.status_code} от Fibbee даже после повторного логина — "
                    "проверь FIBBEE_EMAIL/FIBBEE_PASSWORD в .env."
                )

            if resp.status_code >= 500:
                last_exc = Exception(f"{resp.status_code}: {resp.text[:500]}")
                time.sleep(3 * attempt)
                continue

            resp.raise_for_status()

            if raw:
                return resp.text

            resp_data = resp.json()
            if isinstance(resp_data, dict) and resp_data.get("success") is False:
                raise Exception(f"Fibbee вернул success=false: {resp_data}")
            return resp_data

        raise last_exc

    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        return self._request("GET", path, params=params, retries=retries)

    def _post(self, path: str, data: dict | None = None, retries: int = 3):
        return self._request("POST", path, data=data, retries=retries)

    # ---- комплексы (sales points) ------------------------------------------

    def get_sales_points(self, retries: int = 3) -> list[dict]:
        """GET /v1/sales-points/list — полный список комплексов со статусами."""
        data = self._get("/v1/sales-points/list", retries=retries)
        return data.get("salesPoints", [])

    def get_sales_point_healthchecks(self, retries: int = 3) -> dict:
        """GET /v1/sales-points/healthchecks — остатки по менюайтемам на каждой точке."""
        data = self._get("/v1/sales-points/healthchecks", retries=retries)
        return data.get("healthchecks", {})

    def get_supervisor_config(self, sales_point_id: str, retries: int = 3) -> dict:
        """GET /v1/supervisor/config?salesPointId=... — список подсистем комплекса и их статус (1=ок)."""
        data = self._get("/v1/supervisor/config", params={"salesPointId": sales_point_id}, retries=retries)
        return data.get("payload", {})

    def get_dashboard_html(self, sales_point_id: str, retries: int = 3) -> str:
        """
        GET /dashboard/view/{salesPointId}/dashboard.html?token=<JWT>

        ИСПРАВЛЕНО после первой версии этого докстринга (которая была
        неверной — оставляю объяснение, т.к. это поучительная ошибка
        discovery): изначально показалось, что это статический
        сервер-рендеренный HTML со всеми данными (заказы, состояние
        оборудования, логи) в одном документе, потому что при перехвате
        трафика через Playwright после загрузки страницы не было видно
        доп. fetch/xhr/document запросов при переключении вкладок
        Dashboard/Orders/Logs. На самом деле страница — тонкий SPA-шелл
        (`<div id="app">` + `db.js`), а реальные данные подгружаются по
        **WebSocket** на `wsroot = "/dashboard/monitor/{salesPointId}"`,
        который не попадает в fetch/xhr/document и не был замечен. Прямой
        GET этого URL без браузера возвращает только HTML-шелл (~360 байт),
        без единого заказа — проверено (см. README.md, раздел "Известные
        ограничения"). Открытие в реальном браузере (как в UI) продолжает
        работать, потому что там JS исполняется и WS-соединение
        устанавливается.

        Все данные, которые видно на этом дашборде (история заказов с их
        productDump, состояние точки), доступны через чистые REST-эндпоинты
        без WebSocket — get_orders(sales_point_id=...) и
        get_sales_point_healthchecks()/get_supervisor_config() — их и
        использовать для мониторинга/анализа. Live-телеметрия оборудования
        (лифты выдачи, манипуляторы, диспенсер стаканов и т.п. в реальном
        времени) доступна только через этот WebSocket и НЕ реализована —
        отдельная задача, если понадобится именно потоковая телеметрия, а
        не периодический снимок через REST.

        Метод оставлен как есть (возвращает HTML-шелл) — полезен минимум
        как health-check "у комплекса вообще есть дашборд" (200 с валидным
        HTML vs ошибка).
        """
        return self._request(
            "GET",
            f"/dashboard/view/{sales_point_id}/dashboard.html",
            params={"token": self.token},
            retries=retries,
            raw=True,
        )

    def update_sales_point(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/sales-points/update — данные комплекса (data должен содержать salesPointId)."""
        return self._post("/v1/sales-points/update", data=data, retries=retries)

    # ---- заказы (orders) — история + логи (productDump) --------------------

    def get_orders(self, sales_point_id: str | None = None, order_id: str | None = None,
                    start_date: str | None = None, end_date: str | None = None,
                    status: str | None = None, limit: int = 1000, offset: int = 0,
                    retries: int = 3) -> dict:
        """
        GET /v1/orders/list — история заказов. Каждый заказ содержит
        `productDump` — сырую телеметрию варки конкретного напитка (nozzle,
        milkTemp, waterQnty, cakePressFinal, drinkWeight, extractTime,
        productsConsumption и т.п.) — это и есть "лог заказа" для анализа.

        Подтверждённые вживую параметры (проверено curl_cffi на боевых
        данных, не догадка по аналогии):
        - salesPointId — фильтр по комплексу.
        - orderId — точечный запрос одного заказа (total=1 при точном совпадении).
        - startDate/endDate — фильтр по дате (ISO-строка, например "2026-07-18"),
          РЕАЛЬНО фильтрует (проверено: total упал с 92975 до 25 на тестовом
          запросе) — НЕ dateFrom/dateTo, НЕ from/to (эти имена сервер молча
          игнорирует, total не меняется).
        - limit/offset — пагинация. Без limit сервер отдаёт 1000 (это и есть
          дефолтный лимит, не "все записи"). Фронтенд при массовом экспорте
          (Reports → Export productDump) листает offset += 1000 до
          offset < 100000 — вероятно, серверный кап где-то в этом районе,
          отдельно не подтверждали.
        - status — принят сервером без ошибки, но эффект на total не
          проверяли отдельно (не путать с подтверждённостью выше).

        Возвращает {"success": true, "total": <int>, "orders": [...]}
        целиком (не только список) — total нужен для пагинации по датам.
        """
        params: dict = {"limit": limit, "offset": offset}
        if sales_point_id:
            params["salesPointId"] = sales_point_id
        if order_id:
            params["orderId"] = order_id
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if status:
            params["status"] = status
        return self._get("/v1/orders/list", params=params, retries=retries)

    def get_order(self, order_id: str, retries: int = 3) -> dict | None:
        """Один заказ целиком (включая productDump) по orderId. None, если не найден."""
        data = self.get_orders(order_id=order_id, retries=retries)
        orders = data.get("orders", [])
        return orders[0] if orders else None

    def refund_order(self, data: dict, retries: int = 3) -> dict:
        """
        POST /v1/orders/refund — реальный возврат денег покупателю.
        НЕ ПРОВЕРЕНО НА БОЕВЫХ ЗАКАЗАХ при разработке коннектора — это
        финансовая операция, тестировать только на подтверждённом заказе.
        """
        return self._post("/v1/orders/refund", data=data, retries=retries)

    def update_order_review(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/order-reviews/update."""
        return self._post("/v1/order-reviews/update", data=data, retries=retries)

    # ---- тикеты (service tickets) -------------------------------------------

    def get_tickets(self, sales_point_id: str | None = None, start_date: str | None = None,
                     end_date: str | None = None, limit: int = 1000, offset: int = 0,
                     retries: int = 3) -> list[dict]:
        """
        GET /v1/tickets/list — тикеты раздела "Сервис" (технические
        инциденты: node/zone/status/priority/category, время
        инцидента/закрытия, ссылка на обсуждение в Discord).

        ВАЖНО (проверено контрольным сравнением, не предположение по
        аналогии с orders): salesPointId работает как фильтр, а
        startDate/endDate сервер **молча игнорирует** — узкий диапазон,
        widе диапазон и вообще без дат дают идентичный результат. Список
        отсортирован по убыванию createdAt (новые первыми). Если нужен
        конкретный период — паджинировать offset+limit и останавливаться
        самостоятельно, когда createdAt элемента уйдёт раньше нужной даты
        (см. sync.py::_sync_tickets_window).
        """
        params: dict = {"limit": limit, "offset": offset}
        if sales_point_id:
            params["salesPointId"] = sales_point_id
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        data = self._get("/v1/tickets/list", params=params, retries=retries)
        return data.get("tickets", [])

    def create_ticket(self, data: dict, retries: int = 3) -> dict:
        """
        POST /v1/tickets/create.

        ВНИМАНИЕ: судя по полю discordLink в ответах get_tickets() и
        slackUrl-вебхуку в конфиге точки, тикеты уходят в Discord-канал
        поддержки — тестовый тикет реально уведомит команду. Не вызывать
        без подтверждения пользователя (в отличие от Service Desk, где
        тестовые заявки были безопасны).
        """
        return self._post("/v1/tickets/create", data=data, retries=retries)

    def update_ticket(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/tickets/update — data должен содержать ticketId. См. предупреждение в create_ticket."""
        return self._post("/v1/tickets/update", data=data, retries=retries)

    # ---- логи изменений и инцидентов ----------------------------------------

    def get_changes_log(self, sales_point_id: str | None = None, start_date: str | None = None,
                         end_date: str | None = None, limit: int = 1000, offset: int = 0,
                         retries: int = 3) -> list[dict]:
        """
        GET /v1/changes-log/list — общий аудит-лог изменений сущностей
        (наблюдали type="ticket", но по структуре общий: object/changes
        {added,deleted,updated}/changeId/objectId/changedBy/updatedAt).

        ВАЖНО (проверено контрольным сравнением): как и у tickets/list,
        startDate/endDate здесь **не действуют** — сервер отдаёт одну и ту
        же выборку независимо от диапазона. Отсортировано по убыванию
        updatedAt. Для окна по датам — самостоятельная пагинация с ранней
        остановкой (см. sync.py).
        """
        params: dict = {"limit": limit, "offset": offset}
        if sales_point_id:
            params["salesPointId"] = sales_point_id
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        data = self._get("/v1/changes-log/list", params=params, retries=retries)
        return data.get("changes", [])

    def get_incidents(self, sales_point_id: str | None = None, limit: int = 1000,
                       offset: int = 0, retries: int = 3) -> list[dict]:
        """
        GET /v1/incidents/list — журнал инцидентов ("Журнал инцидентов" на
        карточке комплекса). Каждый инцидент содержит трассировку (`trace`)
        вплоть до конкретного заказа и рецепта — полезно для диагностики,
        почему заказ не удался. БЕЗ salesPointId запрос был замечен
        подвисающим на таймауте (>20с) — всегда передавать фильтр.
        """
        if not sales_point_id:
            raise ValueError("get_incidents требует sales_point_id — без фильтра запрос зависает на сервере")
        params = {"salesPointId": sales_point_id, "limit": limit, "offset": offset}
        data = self._get("/v1/incidents/list", params=params, retries=retries)
        return data.get("incidents", [])

    # ---- супервизор (прямое управление оборудованием) -----------------------

    def supervisor_device_up(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/supervisor/device/up — включить устройство. НЕ ПРОВЕРЕНО на боевом оборудовании."""
        return self._post("/v1/supervisor/device/up", data=data, retries=retries)

    def supervisor_device_down(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/supervisor/device/down — выключить устройство. НЕ ПРОВЕРЕНО на боевом оборудовании."""
        return self._post("/v1/supervisor/device/down", data=data, retries=retries)

    def supervisor_startup(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/supervisor/power-manager/startup. НЕ ПРОВЕРЕНО на боевом оборудовании."""
        return self._post("/v1/supervisor/power-manager/startup", data=data, retries=retries)

    def supervisor_shutdown(self, data: dict, retries: int = 3) -> dict:
        """POST /v1/supervisor/power-manager/shutdown. НЕ ПРОВЕРЕНО на боевом оборудовании."""
        return self._post("/v1/supervisor/power-manager/shutdown", data=data, retries=retries)

    # ---- справочники (для контекста при анализе) ----------------------------

    def get_franchisees(self, retries: int = 3) -> list[dict]:
        data = self._get("/v1/franchisees/list", retries=retries)
        return data.get("franchisees", [])

    def get_warehouses(self, retries: int = 3) -> list[dict]:
        data = self._get("/v1/warehouses/list", retries=retries)
        return data.get("warehouses", [])

    def get_devices(self, retries: int = 3) -> list[dict]:
        data = self._get("/v1/devices/list", retries=retries)
        return data.get("devices", [])

    def get_products(self, retries: int = 3) -> list[dict]:
        data = self._get("/v1/products/list", retries=retries)
        return data.get("products", [])
