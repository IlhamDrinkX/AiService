"""
Тонкий клиент для внутреннего JSON API Service Desk (sd.drinkx.tech).

Открытого API нет "официально" — это внутренние эндпоинты SPA, найденные
через DevTools (чтение) и через перехват сетевых запросов в реальном Chrome
пользователя, управляемом через CDP/Playwright (запись, 2026-07-19) —
см. PROGRESS.md за эту дату. Аутентификация — обычная cookie-сессия после
Google SSO (домен drinkx.tech): один cookie `sd_session`, никаких
дополнительных токенов/заголовков. Получается вручную из DevTools рабочей
вкладки, см. import_cookie.py и README.

Известные эндпоинты чтения:
  GET /api/prototype/tickets              -> полный список заявок (вложения base64 внутри)
  GET /api/auth/me                        -> {authenticated, user: {...}} текущий пользователь
  GET /api/clients                        -> список клиентов
  GET /api/prototype/objects              -> объекты/точки
  GET /api/prototype/complexes            -> комплексы оборудования
  GET /api/prototype/employees            -> сотрудники (инженеры и т.д.)
  GET /api/prototype/problems             -> справочник проблем
  GET /api/prototype/dictionary-groups    -> прочие справочники (статусы/приоритеты и т.п.)

Запись — ДВА разных паттерна в этом API, перепроверено на практике 2026-07-19
для каждой сущности отдельно (ничего не предполагалось по аналогии):

1. "prototype"-сущности (tickets, objects, complexes, employees, problems,
   dictionary-groups) — единый паттерн:
     PUT /api/prototype/<collection>/{id}   body: {"payload": {...ПОЛНЫЙ объект...}}
   full-replace, а не patch: сервер не мержит частичные поля — ждёт весь
   объект целиком (те же ключи, что отдаёт GET-список). id генерируется на
   КЛИЕНТЕ (см. _gen_id) — сервер не проверяет формат и не назначает свой.
   Тот же PUT используется и для создания (id ещё не существовал), и для
   обновления (id уже существует) — сервер не различает эти случаи.
   Для tickets есть дополнительная механика поверх этого паттерна: клиент
   сам ведёт audit[] (история статусов) и сам вычисляет code (SD-xxxx) —
   см. put_ticket/_next_ticket_code ниже, это НЕ общее свойство паттерна,
   специфика именно тикетов.
   dictionary-groups — частный случай: id в пути это id ГРУППЫ
   ("work-types"/"failure-reasons"/"nodes"), а не элемента справочника;
   добавление/изменение одного значения = PUT всей группы целиком с
   изменённым items[] (см. add_dictionary_item).

2. "clients" — обычный REST, ДРУГОЙ паттерн (не prototype):
     POST /api/clients            body: {...без id...}  -> сервер сам назначает id (cuid)
     PUT  /api/clients/{id}       body: {...полный объект с этим id...}
   Без обёртки {"payload": ...} — тело запроса это сразу объект клиента.

GET одной записи по id (симметрично REST) не существует ни для одной
"prototype"-сущности — проверено на tickets, сервер отвечает чистым 404
{"message":"Cannot GET ..."}. Единственный способ прочитать актуальное
состояние — полный список (get_tickets()/get_objects()/... ) и найти
нужный id; update-методы ниже по умолчанию делают такой запрос перед PUT,
если не передан base_*.

Привязка проблемы к заявке — НЕ отдельный эндпоинт, а обычное поле
ticket.problemId (id из get_problems()), проставляется тем же PUT, что и
любое другое изменение заявки — см. link_problem().

Почему curl_cffi, а не httpx/requests: sd.drinkx.tech, как и tracker.drinkx.tech,
режет TLS-хендшейк для нестандартных клиентов — httpx с обычным `ssl`-модулем
Python зависает намертво на do_handshake() и никогда не получает ответ (не
таймаут, а именно вечное подвисание, снимается только Ctrl+C). Ручная проверка
через `fetch()` в консоли браузера этого не показывала, потому что шла с
TLS-отпечатком настоящего Chrome — у Python-клиента отпечаток другой, и его
режут раньше, чем дело доходит до HTTP-уровня. curl_cffi с impersonate="chrome"
воспроизводит нужный отпечаток и проходит эту проверку (тот же фикс, что и в
connectors/tracker/client.py).
"""

import base64
import json
import mimetypes
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cffi_requests

TIMEOUT_SECONDS = 60  # тикеты приходят с base64-вложениями внутри JSON, ответ может быть многомегабайтным
IMPERSONATE = "chrome"


class ServiceDeskAuthError(Exception):
    """Сессия истекла/невалидна — нужно обновить cookie через import_cookie.py."""


class ServiceDeskWriteError(Exception):
    """PUT дошёл до сервера, но тот его отклонил (4xx/5xx, не связанный с авторизацией)."""


class ServiceDeskTicketNotFound(Exception):
    """update_ticket/set_status/... не смогли найти заявку с таким id в текущем get_tickets()."""


class ServiceDeskClient:
    def __init__(self, base_url: str, state_path: str):
        self.base_url = base_url.rstrip("/")
        cookies = self._load_cookies(state_path)
        self.session = cffi_requests.Session(impersonate=IMPERSONATE)
        self.session.cookies.update(cookies)

    @staticmethod
    def _load_cookies(state_path: str) -> dict[str, str]:
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            raise ServiceDeskAuthError(
                f"Не найден файл сессии {state_path}. Запусти import_cookie.py — там нужно "
                "вставить cookie из DevTools уже залогиненной вкладки sd.drinkx.tech."
            )
        return {c["name"]: c["value"] for c in state.get("cookies", [])}

    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(
                    f"{self.base_url}{path}", params=params, timeout=TIMEOUT_SECONDS
                )
            except cffi_requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(3 * attempt)
                continue

            if resp.status_code in (401, 403):
                raise ServiceDeskAuthError(
                    f"{resp.status_code} от Service Desk — сессия истекла или отозвана. "
                    "Обнови cookie через import_cookie.py."
                )
            # SPA на неавторизованный запрос иногда просто редиректит на страницу логина
            # (Google SSO) вместо чистого 401 — в этом случае Content-Type будет не JSON.
            if "application/json" not in resp.headers.get("content-type", ""):
                raise ServiceDeskAuthError(
                    "Ответ не JSON — похоже, запрос увели на страницу логина. "
                    "Сессия протухла, нужно обновить cookie через import_cookie.py."
                )
            if resp.status_code >= 500:
                last_exc = Exception(f"{resp.status_code}: {resp.text[:500]}")
                time.sleep(3 * attempt)
                continue

            resp.raise_for_status()
            return resp.json()

        raise last_exc

    def get_tickets(self, retries: int = 3) -> list[dict]:
        return self._get("/api/prototype/tickets", retries=retries)

    def get_auth_status(self) -> dict:
        return self._get("/api/auth/me")

    def get_clients(self) -> list[dict]:
        return self._get("/api/clients")

    def get_objects(self) -> list[dict]:
        return self._get("/api/prototype/objects")

    def get_complexes(self) -> list[dict]:
        return self._get("/api/prototype/complexes")

    def get_employees(self) -> list[dict]:
        return self._get("/api/prototype/employees")

    def get_problems(self) -> list[dict]:
        return self._get("/api/prototype/problems")

    def get_dictionary_groups(self) -> list[dict]:
        return self._get("/api/prototype/dictionary-groups")

    def get_tickets_in_range(
        self, start: str | datetime | None = None, end: str | datetime | None = None,
        *, date_field: str = "incidentAt", tickets: list[dict] | None = None,
    ) -> list[dict]:
        """Заявки за диапазон дат (по умолчанию по incidentAt — времени
        инцидента; можно передать date_field="createdAt"). start/end — ISO-строки
        или datetime, любая из границ может быть None (открытый диапазон).
        Без start и end — просто полный список (то же самое, что get_tickets()).
        Фильтрация клиентская: отдельного query-параметра на сервере не нашли,
        сервер всегда отдаёт весь список целиком."""
        tickets = tickets if tickets is not None else self.get_tickets()
        if start is None and end is None:
            return tickets

        def _parse(v):
            if v is None:
                return None
            if isinstance(v, datetime):
                dt = v
            else:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            # incidentAt/createdAt в тикетах всегда UTC-aware ("...Z") — если
            # границу диапазона задали как naive ("2026-07-01"), считаем её тоже UTC,
            # иначе Python падает на сравнении naive/aware datetime.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        start_dt, end_dt = _parse(start), _parse(end)
        out = []
        for t in tickets:
            raw = t.get(date_field)
            if not raw:
                continue
            try:
                dt = _parse(raw)
            except ValueError:
                continue
            if start_dt and dt < start_dt:
                continue
            if end_dt and dt > end_dt:
                continue
            out.append(t)
        return out

    # ------------------------------------------------------------------
    # Запись. Два паттерна — см. большой комментарий в шапке файла:
    # "prototype"-сущности (_put, full-replace по id, id генерирует клиент)
    # и "clients" (_post_plain/_put_plain, обычный REST без обёртки payload,
    # id назначает сервер). Всё ниже построено поверх этих низкоуровневых
    # методов; update-хелперы сами читают текущее состояние (если не передан
    # base_*), применяют изменение и делают запись.
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, json_body, retries: int = 3) -> dict:
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    headers={"Referer": f"{self.base_url}/"},
                    timeout=TIMEOUT_SECONDS,
                )
            except cffi_requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(3 * attempt)
                continue

            if resp.status_code in (401, 403):
                raise ServiceDeskAuthError(
                    f"{resp.status_code} от Service Desk при записи — сессия истекла или отозвана. "
                    "Обнови cookie через import_cookie.py."
                )
            if "application/json" not in resp.headers.get("content-type", ""):
                raise ServiceDeskAuthError(
                    f"Ответ на {method} не JSON — похоже, запрос увели на страницу логина. "
                    "Сессия протухла, нужно обновить cookie через import_cookie.py."
                )
            if resp.status_code >= 500:
                last_exc = Exception(f"{resp.status_code}: {resp.text[:500]}")
                time.sleep(3 * attempt)
                continue
            if resp.status_code >= 400:
                raise ServiceDeskWriteError(f"{method} {path} -> {resp.status_code}: {resp.text[:1000]}")

            return resp.json()

        raise last_exc

    def _put(self, path: str, payload: dict, retries: int = 3) -> dict:
        """PUT для 'prototype'-сущностей — тело оборачивается в {"payload": ...}."""
        return self._request("PUT", path, {"payload": payload}, retries=retries)

    def _post_plain(self, path: str, body: dict, retries: int = 3) -> dict:
        """POST для 'clients' — тело как есть, без обёртки."""
        return self._request("POST", path, body, retries=retries)

    def _put_plain(self, path: str, body: dict, retries: int = 3) -> dict:
        """PUT для 'clients' — тело как есть, без обёртки."""
        return self._request("PUT", path, body, retries=retries)

    @staticmethod
    def _gen_id(prefix: str) -> str:
        # Формат подсмотрен у самой SPA (<prefix>-<8 hex>-<6 hex>) — сервер не
        # валидирует формат id для prototype-сущностей, это просто ключ
        # ресурса, но держим тот же стиль на случай скрытой regex-проверки.
        return f"{prefix}-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"

    @staticmethod
    def _gen_ticket_id() -> str:
        return ServiceDeskClient._gen_id("ticket")

    @staticmethod
    def _now_hhmm() -> str:
        # Аудит-записи в SD хранят только локальное HH:MM (см. примеры в
        # PROGRESS.md), без даты и без таймзоны — сервер это не нормализует,
        # просто сохраняет то, что прислал клиент.
        return datetime.now().strftime("%H:%M")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _human_size(num_bytes: int) -> str:
        if num_bytes < 1024:
            return f"{num_bytes} B"
        if num_bytes < 1024 * 1024:
            return f"{round(num_bytes / 1024)} KB"
        return f"{round(num_bytes / 1024 / 1024, 1)} MB"

    def put_ticket(self, ticket_id: str, payload: dict) -> dict:
        """Низкоуровневый вызов. payload должен быть ПОЛНЫМ объектом заявки —
        частичные словари молча затрут остальные поля пустыми значениями на
        сервере. Используй create_ticket/update_ticket вместо прямого вызова,
        если не уверен(а), что уже собрал(а) полный объект."""
        return self._put(f"/api/prototype/tickets/{ticket_id}", payload)

    def _next_ticket_code(self, tickets: list[dict] | None = None) -> str:
        # ВАЖНО (проверено на практике, 2026-07-19): код SD-xxxx назначает НЕ
        # сервер — сервер просто хранит то, что прислали в payload.code.
        # Реальная SPA сама вычисляет следующий код на клиенте перед PUT
        # (перехват показал уже готовый "code":"SD-1027" в теле запроса, а
        # не в ответе). Первая попытка без этой логики создала заявку с
        # пустым code, которая в UI отображается без номера — воспроизводим
        # то же вычисление: максимальный существующий SD-номер + 1.
        tickets = tickets if tickets is not None else self.get_tickets()
        max_num = 0
        for t in tickets:
            code = t.get("code") or ""
            if code.startswith("SD-"):
                try:
                    max_num = max(max_num, int(code[3:]))
                except ValueError:
                    continue
        return f"SD-{max_num + 1}"

    def create_ticket(
        self,
        *,
        complex_name: str,
        object_name: str,
        client_name: str,
        node: str,
        title: str,
        description: str = "",
        severity: str = "Функционал не ограничен",
        priority: str = "Средний",
        incident_at: str | None = None,
        actor: str = "AI-агент",
        code: str | None = None,
    ) -> dict:
        """Создаёт новую заявку L1 (та же форма, что и кнопка "Новая заявка" в UI).
        code вычисляется автоматически (следующий свободный SD-xxxx), если не задан явно —
        передавай явно только если знаешь, что делаешь (риск коллизии при параллельном создании)."""
        ticket_id = self._gen_ticket_id()
        now_iso = self._now_iso()
        tickets_snapshot = self.get_tickets()
        payload = {
            "id": ticket_id,
            "code": code or self._next_ticket_code(tickets_snapshot),
            "createdAt": now_iso,
            "incidentAt": incident_at or now_iso,
            "closedAt": "",
            "createdBy": actor,
            "status": "Новая L1",
            "severity": severity,
            "priority": priority,
            "engineer": None,
            "complexes": [complex_name],
            "object": object_name,
            "client": client_name,
            "node": node,
            "title": title,
            "description": description,
            "l1Actions": "",
            "diagnosis": "",
            "failureReason": "",
            "workDone": "",
            "warranty": None,
            "materials": "",
            "engineerComment": "",
            "workResult": None,
            "problemId": "",
            "actNumber": "",
            "paperActReceived": False,
            "hasSignedActPhoto": False,
            "files": [],
            "relatedTickets": [],
            "audit": [{"actor": actor, "at": self._now_hhmm(), "event": "Создана заявка"}],
        }
        return self.put_ticket(ticket_id, payload)

    def _get_ticket_by_id(self, ticket_id: str) -> dict:
        for t in self.get_tickets():
            if t.get("id") == ticket_id:
                return t
        raise ServiceDeskTicketNotFound(
            f"Заявка {ticket_id} не найдена в текущем get_tickets() — проверь id "
            "(это внутренний id вида 'ticket-...', а не человекочитаемый код SD-xxxx)."
        )

    def update_ticket(
        self,
        ticket_id: str,
        changes: dict,
        *,
        event: str,
        diff: str | None = None,
        actor: str = "AI-агент",
        base_ticket: dict | None = None,
    ) -> dict:
        """Общий механизм обновления: читает текущую заявку (если не передан
        base_ticket — например, свежий объект из недавнего get_tickets()),
        накатывает changes поверх, дописывает запись в audit и делает PUT.
        Именно так это делает сама SPA — см. комментарий в шапке файла."""
        base = dict(base_ticket) if base_ticket is not None else dict(self._get_ticket_by_id(ticket_id))
        base.update(changes)
        audit_entry = {"actor": actor, "at": self._now_hhmm(), "event": event}
        if diff is not None:
            audit_entry["diff"] = diff
        base["audit"] = list(base.get("audit") or []) + [audit_entry]
        return self.put_ticket(ticket_id, base)

    def set_status(
        self, ticket_id: str, new_status: str, *, actor: str = "AI-агент", base_ticket: dict | None = None
    ) -> dict:
        base = dict(base_ticket) if base_ticket is not None else dict(self._get_ticket_by_id(ticket_id))
        old_status = base.get("status")
        return self.update_ticket(
            ticket_id,
            {"status": new_status},
            event="Статус изменён",
            diff=f"{old_status} -> {new_status}",
            actor=actor,
            base_ticket=base,
        )

    def assign_engineer(
        self, ticket_id: str, engineer_name: str, *, actor: str = "AI-агент", base_ticket: dict | None = None
    ) -> dict:
        return self.update_ticket(
            ticket_id,
            {"engineer": engineer_name, "status": "Назначен инженер"},
            event="Назначен инженер",
            diff=engineer_name,
            actor=actor,
            base_ticket=base_ticket,
        )

    def add_l1_action(
        self, ticket_id: str, text: str, *, actor: str = "AI-агент", base_ticket: dict | None = None
    ) -> dict:
        return self.update_ticket(
            ticket_id, {"l1Actions": text}, event="Действия L1 изменены", diff=text, actor=actor, base_ticket=base_ticket
        )

    def add_diagnosis(
        self, ticket_id: str, text: str, *, actor: str = "AI-агент", base_ticket: dict | None = None
    ) -> dict:
        return self.update_ticket(
            ticket_id, {"diagnosis": text}, event="Диагностика изменена", diff=text, actor=actor, base_ticket=base_ticket
        )

    def add_engineer_comment(
        self, ticket_id: str, text: str, *, actor: str = "AI-агент", base_ticket: dict | None = None
    ) -> dict:
        """Ближайший аналог "добавить комментарий" в этой модели данных —
        отдельного треда комментариев в API нет, есть одно поле
        engineerComment (как в форме "Работы и акт" в UI)."""
        return self.update_ticket(
            ticket_id,
            {"engineerComment": text},
            event="Добавлен комментарий инженера",
            diff=text,
            actor=actor,
            base_ticket=base_ticket,
        )

    def link_problem(
        self, ticket_id: str, problem_id: str | None, *, actor: str = "AI-agent", base_ticket: dict | None = None
    ) -> dict:
        """Привязывает заявку к проблеме (Работы и акт -> Корневая проблема ->
        Выбрать в UI) — это просто поле problemId, отдельного эндпоинта нет.
        problem_id=None отвязывает (соответствует кнопке "Отвязать" в UI).
        problem_id — это id из get_problems(), НЕ название."""
        base = dict(base_ticket) if base_ticket is not None else dict(self._get_ticket_by_id(ticket_id))
        if problem_id is None:
            return self.update_ticket(
                ticket_id, {"problemId": ""}, event="Проблема отвязана", actor=actor, base_ticket=base
            )
        problems = {p["id"]: p for p in self.get_problems()}
        title = problems.get(problem_id, {}).get("title", problem_id)
        return self.update_ticket(
            ticket_id, {"problemId": problem_id}, event="Привязана проблема", diff=title, actor=actor, base_ticket=base
        )

    def upload_file(
        self, ticket_id: str, file_path: str, *, actor: str = "AI-агент", base_ticket: dict | None = None
    ) -> dict:
        """Загружает файл (фото акта, диагностика и т.п.) — сервер не отдаёт
        отдельный upload-эндпоинт, вложение просто кладётся base64-строкой в
        files[].dataUrl того же PUT-запроса, что и любое другое изменение."""
        p = Path(file_path)
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"

        base = dict(base_ticket) if base_ticket is not None else dict(self._get_ticket_by_id(ticket_id))
        files = list(base.get("files") or []) + [
            {"name": p.name, "type": mime, "size": self._human_size(len(data)), "dataUrl": data_url}
        ]
        return self.update_ticket(
            ticket_id, {"files": files}, event="Приложены материалы", diff=p.name, actor=actor, base_ticket=base
        )

    # ------------------------------------------------------------------
    # Объекты (точки клиента). "prototype"-паттерн, id генерирует клиент.
    # ------------------------------------------------------------------

    def _find_by_id(self, items: list[dict], entity_id: str, kind: str) -> dict:
        for it in items:
            if it.get("id") == entity_id:
                return it
        raise ServiceDeskTicketNotFound(f"{kind} {entity_id} не найден(а) в текущем списке.")

    def create_object(
        self, *, client_name: str, name: str, address: str = "", work_hours: str = "09:00-22:00",
        access_rules: str = "по заявке", contact: str = "", complexes: list[str] | None = None, comment: str = "",
    ) -> dict:
        object_id = self._gen_id("object")
        payload = {
            "id": object_id, "client": client_name, "name": name, "address": address,
            "workHours": work_hours, "accessRules": access_rules, "contact": contact,
            "complexes": complexes or [], "comment": comment,
        }
        return self._put(f"/api/prototype/objects/{object_id}", payload)

    def update_object(self, object_id: str, changes: dict, *, base_object: dict | None = None) -> dict:
        base = dict(base_object) if base_object is not None else dict(self._find_by_id(self.get_objects(), object_id, "Объект"))
        base.update(changes)
        return self._put(f"/api/prototype/objects/{object_id}", base)

    # ------------------------------------------------------------------
    # Комплексы оборудования. "prototype"-паттерн.
    # ------------------------------------------------------------------

    def create_complex(
        self, *, serial: str, client_name: str = "", object_name: str = "", version: str = "",
        production_date: str = "", commissioning_date: str = "", water_supply: str = "", drainage: str = "",
        internet: str = "", is_test_stand: bool = False, modules: str = "", comment: str = "",
    ) -> dict:
        complex_id = self._gen_id("complex")
        payload = {
            "id": complex_id, "serial": serial, "version": version, "client": client_name, "object": object_name,
            "productionDate": production_date, "commissioningDate": commissioning_date, "waterSupply": water_supply,
            "drainage": drainage, "internet": internet, "isTestStand": is_test_stand, "modules": modules,
            "comment": comment,
        }
        return self._put(f"/api/prototype/complexes/{complex_id}", payload)

    def update_complex(self, complex_id: str, changes: dict, *, base_complex: dict | None = None) -> dict:
        base = dict(base_complex) if base_complex is not None else dict(
            self._find_by_id(self.get_complexes(), complex_id, "Комплекс")
        )
        base.update(changes)
        return self._put(f"/api/prototype/complexes/{complex_id}", base)

    # ------------------------------------------------------------------
    # Сотрудники. "prototype"-паттерн. role — внутренний код (наблюдалось:
    # "observer" для роли "Наблюдатель"; "Инженер"/"Координатор" в UI —
    # значения role для них не проверяли, узнать так же через DevTools при
    # необходимости — отредактировать реального сотрудника и посмотреть тело
    # PUT-запроса).
    # ------------------------------------------------------------------

    def create_employee(
        self, *, last_name: str, first_name: str, middle_name: str = "", phone: str = "", email: str = "",
        role: str = "observer", schedule: str = "5/2, 09:00-18:00", presence_status: str = "работает", comment: str = "",
    ) -> dict:
        employee_id = self._gen_id("employee")
        full_name = " ".join(p for p in (last_name, first_name) if p)
        payload = {
            "id": employee_id, "lastName": last_name, "firstName": first_name, "middleName": middle_name,
            "fullName": full_name, "phone": phone, "email": email, "role": role, "schedule": schedule,
            "presenceStatus": presence_status, "comment": comment,
        }
        return self._put(f"/api/prototype/employees/{employee_id}", payload)

    def update_employee(self, employee_id: str, changes: dict, *, base_employee: dict | None = None) -> dict:
        base = dict(base_employee) if base_employee is not None else dict(
            self._find_by_id(self.get_employees(), employee_id, "Сотрудник")
        )
        base.update(changes)
        if "lastName" in changes or "firstName" in changes:
            base["fullName"] = " ".join(p for p in (base.get("lastName", ""), base.get("firstName", "")) if p)
        return self._put(f"/api/prototype/employees/{employee_id}", base)

    # ------------------------------------------------------------------
    # Проблемы (корневые причины). "prototype"-паттерн, самая простая схема
    # (только id/title/description).
    # ------------------------------------------------------------------

    def create_problem(self, *, title: str, description: str = "") -> dict:
        problem_id = self._gen_id("problem")
        payload = {"id": problem_id, "title": title, "description": description}
        return self._put(f"/api/prototype/problems/{problem_id}", payload)

    def update_problem(self, problem_id: str, changes: dict, *, base_problem: dict | None = None) -> dict:
        base = dict(base_problem) if base_problem is not None else dict(
            self._find_by_id(self.get_problems(), problem_id, "Проблема")
        )
        base.update(changes)
        return self._put(f"/api/prototype/problems/{problem_id}", base)

    # ------------------------------------------------------------------
    # Клиенты — ЕДИНСТВЕННАЯ сущность на обычном REST (см. комментарий в
    # шапке файла): POST без обёртки создаёт (сервер сам назначает id),
    # PUT /{id} без обёртки обновляет.
    # ------------------------------------------------------------------

    def create_client(
        self, *, brand_name: str, legal_name: str, inn: str = "", contract_number: str = "",
        contract_date: str | None = None, contract_until: str | None = None, drinkx_entity: str = "ООО Дринкикс",
        contacts: str = "", comment: str = "",
    ) -> dict:
        """contract_date/contract_until — ISO-даты "YYYY-MM-DD"; сервер валидирует
        их как обязательные (проверено на практике: пустая строка -> 400 Bad Request
        "must be a valid ISO 8601 date string"). Если не задать явно — по умолчанию
        сегодня и +365 дней (как безобидный плейсхолдер, поправить потом в UI)."""
        today = datetime.now().date()
        payload = {
            "legalName": legal_name, "brandName": brand_name, "inn": inn, "contractNumber": contract_number,
            "contractDate": contract_date or today.isoformat(),
            "contractUntil": contract_until or today.replace(year=today.year + 1).isoformat(),
            "drinkxEntity": drinkx_entity, "contacts": contacts, "objectsCount": 0, "complexesCount": 0,
            "comment": comment,
        }
        return self._post_plain("/api/clients", payload)

    def _get_client_by_id(self, client_id: str) -> dict:
        return self._find_by_id(self.get_clients(), client_id, "Клиент")

    def update_client(self, client_id: str, changes: dict, *, base_client: dict | None = None) -> dict:
        base = dict(base_client) if base_client is not None else dict(self._get_client_by_id(client_id))
        base.update(changes)
        return self._put_plain(f"/api/clients/{client_id}", base)

    # ------------------------------------------------------------------
    # Справочники (dictionary-groups). ВАЖНО: id в пути — id ГРУППЫ
    # ("work-types", "failure-reasons", "nodes"), а не элемента. Добавление
    # или изменение одного значения = PUT всей группы целиком с новым
    # items[] — сервер не поддерживает точечное изменение одного элемента.
    # ------------------------------------------------------------------

    KNOWN_DICTIONARY_GROUPS = ("work-types", "failure-reasons", "nodes")

    def _get_dictionary_group(self, group_id: str) -> dict:
        for g in self.get_dictionary_groups():
            if g.get("id") == group_id:
                return g
        raise ServiceDeskTicketNotFound(
            f"Группа справочника {group_id!r} не найдена — известные группы: {self.KNOWN_DICTIONARY_GROUPS}"
        )

    def add_dictionary_item(self, group_id: str, *, name: str, description: str = "", active: bool = True) -> dict:
        """group_id — один из KNOWN_DICTIONARY_GROUPS (или другой, если появится новый —
        сервер сам подскажет 404, если группы не существует)."""
        group = dict(self._get_dictionary_group(group_id))
        item_id = self._gen_id("dict")
        items = list(group.get("items") or []) + [
            {"id": item_id, "name": name, "description": description, "active": active}
        ]
        group["items"] = items
        return self._put(f"/api/prototype/dictionary-groups/{group_id}", group)

    def update_dictionary_item(self, group_id: str, item_id: str, changes: dict) -> dict:
        group = dict(self._get_dictionary_group(group_id))
        items = list(group.get("items") or [])
        found = False
        for i, it in enumerate(items):
            if it.get("id") == item_id:
                items[i] = {**it, **changes}
                found = True
                break
        if not found:
            raise ServiceDeskTicketNotFound(f"Значение {item_id} не найдено в группе {group_id}")
        group["items"] = items
        return self._put(f"/api/prototype/dictionary-groups/{group_id}", group)

    def deactivate_dictionary_item(self, group_id: str, item_id: str) -> dict:
        """Мягкое удаление — ставит active=False (сохраняет историю в старых заявках,
        где значение уже использовано). Соответствует чекбоксу "Активно" в UI."""
        return self.update_dictionary_item(group_id, item_id, {"active": False})
