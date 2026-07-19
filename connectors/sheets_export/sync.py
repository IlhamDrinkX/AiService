"""
Пишет открытые (незакрытые) тикеты Fibbee ERP и заявки Service Desk в одну
общую Google-таблицу со ссылками на источник — единый вид для всех, кому
нужно посмотреть, что сейчас в работе, без входа в каждую систему отдельно.

Данные и ссылки берутся из core/reporting/generate.py (_load_fibbee/
_load_servicedesk, open_only=True — см. там же, почему открытость это
state='open' у Fibbee и status!='Закрыта' у Service Desk) — та же логика
и те же (непроверенные, см. link_templates.yaml) шаблоны ссылок, что и в
отчётах, чтобы не разойтись в двух местах.

Таблица создаётся сама при первом запуске (spreadsheets.create — Sheets API
это позволяет без Drive-прав, у коннектора нет scope на Drive вообще) и
ЦЕЛИКОМ перезаписывается на каждом запуске. Открытых тикетов разом немного
(десятки-сотни), upsert по ключу не оправдан лишней сложностью — поэтому в
таблице руками ничего не редактируй, следующий прогон затрёт правки.

После первого запуска — обязательно открой ссылку на таблицу (см. вывод
ниже или sheet_state.json) и расшарь её на нужных людей самостоятельно:
Sheets API даёт только доступ к данным, не к правам доступа — их можно
выставить только из интерфейса Google Таблиц (кнопка "Поделиться").

Запуск: python sync.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(PROJECT_ROOT / "core" / "reporting"))
import generate  # noqa: E402  переиспользуем _load_fibbee/_load_servicedesk/_parse_dt/_MSK

import auth  # noqa: E402

STATE_PATH = ROOT / "sheet_state.json"
SHEET_TITLE = "Открытые тикеты — Fibbee + Service Desk"
TAB_NAME = "Тикеты"
COLUMNS = ["Источник", "№", "Статус", "Заголовок", "Создано", "Ссылка", "Описание"]
DESC_MAX_CHARS = 300


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_dt(raw) -> str:
    dt = generate._parse_dt(raw)
    if not dt:
        return str(raw or "")
    return dt.astimezone(generate._MSK).strftime("%d.%m.%Y %H:%M")


def _ensure_spreadsheet(sheets_service, state: dict) -> str:
    sheet_id = state.get("spreadsheet_id")
    if sheet_id:
        return sheet_id
    body = {
        "properties": {"title": SHEET_TITLE},
        "sheets": [{"properties": {"title": TAB_NAME}}],
    }
    result = sheets_service.spreadsheets().create(
        body=body, fields="spreadsheetId,spreadsheetUrl"
    ).execute()
    sheet_id = result["spreadsheetId"]
    url = result["spreadsheetUrl"]
    state["spreadsheet_id"] = sheet_id
    state["spreadsheet_url"] = url
    state["created_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    print(f"Создана новая таблица: {url}")
    print(
        "Открой её и нажми 'Поделиться' — дай доступ нужным людям/домену, "
        "этот скрипт прав доступа не выставляет (нет Drive-scope, только запись данных)."
    )
    return sheet_id


def _build_rows() -> tuple[list[list], int, int]:
    fibbee = generate._load_fibbee(None, limit=5000, open_only=True)
    servicedesk = generate._load_servicedesk(None, limit=5000, open_only=True)
    items = fibbee + servicedesk
    items.sort(key=lambda it: (it["source"], it.get("created_at") or ""), reverse=True)

    rows = []
    for it in items:
        desc = (it.get("text") or "").replace("\n", " ").strip()
        if len(desc) > DESC_MAX_CHARS:
            desc = desc[:DESC_MAX_CHARS] + "…"
        rows.append(
            [
                it["source"],
                it["ref"],
                it["status"],
                it["title"],
                _format_dt(it.get("created_at")),
                it.get("url") or "",  # обычная строка-URL — Sheets сам превращает её в ссылку
                desc,
            ]
        )
    return rows, len(fibbee), len(servicedesk)


def main():
    services = auth.get_services()
    sheets = services["sheets"]
    state = _load_state()
    sheet_id = _ensure_spreadsheet(sheets, state)

    rows, n_fibbee, n_sd = _build_rows()
    note = (
        f"Обновлено: {datetime.now(generate._MSK).strftime('%d.%m.%Y %H:%M')} МСК — "
        "таблица перезаписывается автоматически при каждой синхронизации, не редактируй вручную"
    )

    # Полная перезапись: сначала чистим весь диапазон данных, потом пишем
    # заново — так закрытые с прошлого раза тикеты гарантированно исчезают,
    # без отдельной логики "удали то, что пропало".
    sheets.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{TAB_NAME}!A1:Z10000"
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{TAB_NAME}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[note], COLUMNS, *rows]},
    ).execute()

    print(f"Готово: {len(rows)} строк (Fibbee открытых: {n_fibbee}, Service Desk открытых: {n_sd})")
    print(f"Таблица: {state.get('spreadsheet_url')}")


if __name__ == "__main__":
    main()
