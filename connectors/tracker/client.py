"""
Тонкий клиент для внутреннего JSON API трекера задач (tracker.drinkx.tech).

История: изначально был только официальный Bearer-токен API с явным scope
`tasks:read` (см. git-историю/README) — чтение досок и задач, без записи и
без привязки к конкретному человеку. По просьбе пользователя (2026-07-19)
переделано по аналогии с connectors/servicedesk: полный read+write через
внутренний JSON API самой SPA, авторизация — обычная cookie-сессия после
Google SSO (домен drinkx.tech), один httpOnly cookie `sid`. Действия идут
от имени залогиненного пользователя (reporter/actor в ответах API — реальный
человек, не сервисный токен), что и даёт "работать от своего имени" и
доступ к персональным уведомлениям.

Эндпоинты найдены перехватом трафика реального Chrome пользователя через
CDP/Playwright (см. [[feedback-endpoint-discovery-technique]] в памяти) плюс
статический разбор фронтенд-бандла (`/assets/index-*.js`) — там весь API
клиент лежит в одном месте открытым текстом (см. PROGRESS.md за 2026-07-19).
Всё ниже проверено живыми запросами на тестовых задачах с пометкой
"ТЕСТ-discovery" (созданы и затем удалены через DELETE, прод не замусорен).

Известные эндпоинты:

Профиль/команда:
  GET  /api/me                      -> текущий пользователь
  GET  /api/team                    -> список сотрудников (для назначения)
  GET  /api/tags                    -> все теги, встречавшиеся в задачах
  GET  /api/focus                   -> "Мой фокус" (задачи текущего пользователя)

Доски/задачи:
  GET  /api/task-types              -> список досок (DEV/TEST/CAD/ADM/OPS/LGL/PRC/FIN/PRJ/MFG/SLS...) + их статусы
  GET  /api/tasks?type=<CODE>       -> все задачи доски
  GET  /api/tasks/{id}              -> одна задача по uuid
  GET  /api/tasks/by-code/{code}    -> одна задача по коду (DEV-1234)
  POST /api/tasks                   -> создать задачу
  PATCH /api/tasks/{id}             -> обновить задачу (ЧАСТИЧНЫЙ patch — только переданные поля, не full-replace!)
  DELETE /api/tasks/{id}[?cascade=true] -> удалить задачу (cascade — вместе с подзадачами)
  GET  /api/tasks/{id}/subtasks     -> подзадачи
  GET  /api/tasks/{id}/commits      -> привязанные git-коммиты

Комментарии:
  GET  /api/tasks/{id}/comments     -> комментарии задачи
  POST /api/tasks/{id}/comments     -> добавить комментарий, body: {"body": "..."}
  PATCH /api/comments/{id}          -> отредактировать, body: {"body": "..."}
  DELETE /api/comments/{id}         -> удалить комментарий

Вложения:
  GET  /api/tasks/{id}/attachments  -> список вложений
  POST /api/tasks/{id}/attachments  -> загрузить файл, multipart/form-data, поле "file"
  DELETE /api/attachments/{id}      -> удалить вложение

Уведомления:
  GET  /api/notifications/summary   -> {unreadCount, tasks: [...]}
  GET  /api/notifications[?status=unread] -> список уведомлений
  POST /api/notifications/read      -> отметить прочитанными, body: {"ids": [...]} или {"commentId": "..."}

Реалтайм (не оборачивается этим клиентом — Server-Sent Events, а не обычный
JSON-эндпоинт; открывать через EventSource в браузере/фронтенде):
  GET  /api/task-events

Не реализовано в этом клиенте (нашли в бандле, не было практической нужды
проверять живыми запросами — можно добавить по тому же паттерну, если
понадобится): /api/releases*, /api/admin/task-types/*/statuses*,
/api/access-requests*, /api/invitations*, /api/me/integrations/tokens*.

Ключевые факты, важные для правильного использования:
- PATCH /api/tasks/{id} — это НАСТОЯЩИЙ частичный patch (не как
  /api/prototype/* в Service Desk): можно прислать только {"statusId": "..."}
  и остальные поля не тронутся. Full-replace тут не нужен и не ожидается.
- POST /api/tasks сам генерирует id (uuid) и человекочитаемый code
  (DEV-1234) на сервере — в отличие от Service Desk, клиенту не нужно
  вычислять следующий номер самому.
- DELETE полноценно работает (в отличие от Service Desk, где вообще нет
  DELETE-эндпоинтов) — проверено на тестовых задачах, отвечает
  {"ok": true}.
- assigneeId/parentTaskId принимают null для снятия назначения/отвязки
  от родителя.
- estimate — объект {"value": <число>, "unit": "hours"|"days"}, не голое число.
- description — HTML-строка (Tiptap/ProseMirror редактор на фронте), не
  голый текст и не markdown; при программной записи можно слать как
  простой `<p>...</p>` или голый текст — сервер не проверял в тестах.

Почему curl_cffi, а не httpx/requests: tracker.drinkx.tech режет TLS-хендшейк
для нестандартных клиентов (см. [[project-vpn-split-tunnel]] и
connectors/servicedesk/client.py — тот же фикс). curl_cffi с
impersonate="chrome" воспроизводит нужный TLS-отпечаток и проходит проверку.
"""

import json
import os
import time
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

TIMEOUT_SECONDS = 30
IMPERSONATE = "chrome"


class TrackerAuthError(Exception):
    """Cookie отсутствует/истекла/отозвана — нужно обновить через import_cookie.py."""


class TrackerNotFoundError(Exception):
    """404 от API (например задача с таким кодом/id не существует)."""


class TrackerClient:
    def __init__(self, base_url: str, state_path: str | None = None, cookie_header: str | None = None):
        """
        Один из двух способов авторизации:
          - state_path: путь к JSON-файлу от import_cookie.py (обычный способ)
          - cookie_header: сырая строка "name1=value1; name2=value2" напрямую
            (удобно для одноразовых скриптов/discovery без файла состояния)
        """
        self.base_url = base_url.rstrip("/")
        self.session = cffi_requests.Session(impersonate=IMPERSONATE)
        domain = urlparse(self.base_url).hostname

        if cookie_header:
            cookies = self._parse_cookie_header(cookie_header)
        elif state_path:
            cookies = self._load_cookies(state_path)
        else:
            raise ValueError("Нужно передать либо state_path, либо cookie_header")

        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=domain)

    @staticmethod
    def _parse_cookie_header(raw: str) -> dict[str, str]:
        out = {}
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            out[name.strip()] = value.strip()
        return out

    @classmethod
    def _load_cookies(cls, state_path: str) -> dict[str, str]:
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            raise TrackerAuthError(
                f"Не найден файл сессии {state_path}. Запусти import_cookie.py — там нужно "
                "вставить cookie из DevTools уже залогиненной вкладки tracker.drinkx.tech."
            )
        return {c["name"]: c["value"] for c in state.get("cookies", [])}

    def _request(self, method: str, path: str, params=None, json_body=None, files=None,
                 retries: int = 3, expect_json: bool = True):
        last_exc = None
        url = f"{self.base_url}{path}"
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, files=files, timeout=TIMEOUT_SECONDS
                )
            except cffi_requests.exceptions.RequestException as e:
                last_exc = e
                time.sleep(3 * attempt)
                continue

            if resp.status_code in (401, 403):
                raise TrackerAuthError(
                    f"{resp.status_code} от трекера — cookie истекла или отозвана. "
                    "Обнови через import_cookie.py."
                )
            if resp.status_code == 404:
                raise TrackerNotFoundError(f"404: {method} {path}")
            if resp.status_code >= 500:
                last_exc = Exception(f"{resp.status_code}: {resp.text[:500]}")
                time.sleep(3 * attempt)
                continue

            ctype = resp.headers.get("content-type", "")
            if expect_json and "application/json" not in ctype:
                raise TrackerAuthError(
                    "Ответ не JSON — похоже, cookie истекла и запрос увели на страницу логина. "
                    "Обнови через import_cookie.py."
                )
            resp.raise_for_status()
            return resp.json() if expect_json else resp

        raise last_exc

    # ---------- профиль / команда ----------

    def get_me(self, retries: int = 3) -> dict:
        return self._request("GET", "/api/me", retries=retries)

    def get_team(self, retries: int = 3) -> list[dict]:
        return self._request("GET", "/api/team", retries=retries)

    def get_tags(self, retries: int = 3) -> list[str]:
        return self._request("GET", "/api/tags", retries=retries)

    def get_focus(self, retries: int = 3) -> list[dict]:
        return self._request("GET", "/api/focus", retries=retries)

    # ---------- доски / задачи (чтение) ----------

    def get_task_types(self, retries: int = 3) -> list[dict]:
        """Список досок (DEV/TEST/CAD/...) + их статусы."""
        return self._request("GET", "/api/task-types", retries=retries)

    def get_tasks(self, board_code: str, retries: int = 3) -> list[dict]:
        return self._request("GET", "/api/tasks", params={"type": board_code}, retries=retries)

    def get_task(self, task_id: str, retries: int = 3) -> dict:
        return self._request("GET", f"/api/tasks/{task_id}", retries=retries)

    def get_task_by_code(self, code: str, retries: int = 3) -> dict:
        return self._request("GET", f"/api/tasks/by-code/{code}", retries=retries)

    def get_subtasks(self, task_id: str, retries: int = 3) -> list[dict]:
        return self._request("GET", f"/api/tasks/{task_id}/subtasks", retries=retries)

    def get_commits(self, task_id: str, retries: int = 3) -> list[dict]:
        return self._request("GET", f"/api/tasks/{task_id}/commits", retries=retries)

    # ---------- задачи (запись) ----------

    def find_board(self, board_code: str) -> dict:
        boards = self.get_task_types()
        for b in boards:
            if b["code"] == board_code:
                return b
        raise TrackerNotFoundError(f"Доска с кодом {board_code!r} не найдена")

    def find_status_id(self, board_code: str, status_name: str) -> str:
        board = self.find_board(board_code)
        for s in board["statuses"]:
            if s["name"] == status_name:
                return s["id"]
        raise TrackerNotFoundError(f"Статус {status_name!r} не найден на доске {board_code!r}")

    def create_task(self, board_code: str, title: str, description: str = "", status_id: str | None = None,
                     assignee_id: str | None = None, is_urgent: bool = False, estimate: dict | None = None,
                     parent_task_id: str | None = None, tags: list[str] | None = None) -> dict:
        """Создать задачу. Если status_id не передан — берётся начальный статус доски (isInitial=true)."""
        if status_id is None:
            board = self.find_board(board_code)
            initial = next((s for s in board["statuses"] if s.get("isInitial")), board["statuses"][0])
            status_id = initial["id"]
        body = {
            "typeCode": board_code,
            "title": title,
            "description": description,
            "statusId": status_id,
            "assigneeId": assignee_id,
            "isUrgent": is_urgent,
            "estimate": estimate,
            "parentTaskId": parent_task_id,
            "tags": tags or [],
        }
        return self._request("POST", "/api/tasks", json_body=body)

    def update_task(self, task_id: str, fields: dict) -> dict:
        """Частичный PATCH — присылай только те поля, что хочешь изменить."""
        return self._request("PATCH", f"/api/tasks/{task_id}", json_body=fields)

    def set_status(self, task_id: str, status_id: str) -> dict:
        return self.update_task(task_id, {"statusId": status_id})

    def set_status_by_name(self, task_id: str, board_code: str, status_name: str) -> dict:
        return self.set_status(task_id, self.find_status_id(board_code, status_name))

    def assign(self, task_id: str, assignee_id: str | None) -> dict:
        """assignee_id=None снимает назначение."""
        return self.update_task(task_id, {"assigneeId": assignee_id})

    def set_urgent(self, task_id: str, urgent: bool = True) -> dict:
        return self.update_task(task_id, {"isUrgent": urgent})

    def set_estimate(self, task_id: str, value: float, unit: str = "hours") -> dict:
        return self.update_task(task_id, {"estimate": {"value": value, "unit": unit}})

    def set_tags(self, task_id: str, tags: list[str]) -> dict:
        return self.update_task(task_id, {"tags": tags})

    def set_parent(self, task_id: str, parent_task_id: str | None) -> dict:
        return self.update_task(task_id, {"parentTaskId": parent_task_id})

    def delete_task(self, task_id: str, cascade: bool = False) -> dict:
        params = {"cascade": "true"} if cascade else None
        resp = self._request("DELETE", f"/api/tasks/{task_id}", params=params, expect_json=False)
        return resp.json()

    # ---------- комментарии ----------

    def get_comments(self, task_id: str, retries: int = 3) -> list[dict]:
        return self._request("GET", f"/api/tasks/{task_id}/comments", retries=retries)

    def add_comment(self, task_id: str, body: str) -> dict:
        return self._request("POST", f"/api/tasks/{task_id}/comments", json_body={"body": body})

    def update_comment(self, comment_id: str, body: str) -> dict:
        return self._request("PATCH", f"/api/comments/{comment_id}", json_body={"body": body})

    def delete_comment(self, comment_id: str) -> None:
        self._request("DELETE", f"/api/comments/{comment_id}", expect_json=False)

    # ---------- вложения ----------

    def get_attachments(self, task_id: str, retries: int = 3) -> list[dict]:
        return self._request("GET", f"/api/tasks/{task_id}/attachments", retries=retries)

    def upload_attachment(self, task_id: str, file_path: str) -> dict:
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            data = f.read()
        resp = self.session.request(
            "POST", f"{self.base_url}/api/tasks/{task_id}/attachments",
            files={"file": (filename, data)}, timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code in (401, 403):
            raise TrackerAuthError("401/403 при загрузке вложения — обнови cookie через import_cookie.py.")
        resp.raise_for_status()
        return resp.json()

    def delete_attachment(self, attachment_id: str) -> None:
        self._request("DELETE", f"/api/attachments/{attachment_id}", expect_json=False)

    # ---------- уведомления ----------

    def get_notifications_summary(self, retries: int = 3) -> dict:
        """{"unreadCount": int, "tasks": [...]}"""
        return self._request("GET", "/api/notifications/summary", retries=retries)

    def get_notifications(self, unread_only: bool = True, retries: int = 3) -> list[dict]:
        params = {"status": "unread"} if unread_only else None
        return self._request("GET", "/api/notifications", params=params, retries=retries)

    def mark_notifications_read(self, ids: list[str]) -> None:
        self._request("POST", "/api/notifications/read", json_body={"ids": ids}, expect_json=False)

    def mark_all_notifications_read(self) -> None:
        summary = self.get_notifications(unread_only=True)
        ids = [n["id"] for n in summary]
        if ids:
            self.mark_notifications_read(ids)
