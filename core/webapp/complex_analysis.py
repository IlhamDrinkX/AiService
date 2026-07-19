"""
AI-анализ состояния комплексов (жёлтый/красный) по данным Fibbee ERP.

Запускается вручную кнопкой на /discord (не фоновым таском) — исходно
рассматривался автозапуск по расписанию, но сознательно отказались: тот же
бесплатный тир corporate Gemini (gemini_direct), на котором уже ловили
HTTP 429 при суммаризации Discord (см. models.yaml/client.py, лимит
~15 запросов/окно), легко исчерпать регулярными фоновыми прогонами по 95+
комплексам. Батчим по BATCH_SIZE комплексов на один вызов модели, чтобы
держать общее число запросов низким (95 комплексов -> ~7 вызовов, а не 95).

Сигналы на вход модели — НЕ сырой product_dump (проверили живые данные:
это просто {menuItemId: count} расхода ингредиентов на заказ, без названий
ингредиентов — для LLM бесполезно и раздувает промпт) и не сырой healthcheck
(тоже {menuItemId: число}, похоже на остатки, но без каталога названий
интерпретировать однозначно нельзя — решили не гадать). Вместо этого:
  - открытые тикеты (fibbee_tickets, state='open'): node/zone/priority/
    description — уже готовый, однозначный сигнал о реальной проблеме;
  - доля неудачных заказов (fibbee_orders.status == 'failed') за последние
    ORDERS_WINDOW заказов комплекса.

Результат кэшируется в JSON (core/webapp/data/complex_analysis.json) —
таблица комплексов на /discord подсвечивается по кэшу при каждой загрузке
страницы без повторного вызова модели; кэш обновляется только по кнопке.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIBBEE_DB = ROOT / "connectors" / "fibbee" / "data" / "fibbee.db"
CACHE_DIR = Path(__file__).resolve().parent / "data"
CACHE_PATH = CACHE_DIR / "complex_analysis.json"

BATCH_SIZE = 10
ORDERS_WINDOW = 30
MAX_TICKETS_PER_COMPLEX = 8
LEVELS = {"critical", "warning", "ok"}


def _connect() -> sqlite3.Connection | None:
    if not FIBBEE_DB.exists():
        return None
    conn = sqlite3.connect(FIBBEE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _gather_signal(conn: sqlite3.Connection, sp: dict) -> dict:
    spid = sp.get("sales_point_id")
    tickets = conn.execute(
        "SELECT node, zone, priority, description FROM fibbee_tickets "
        "WHERE state = 'open' AND sales_point_ids LIKE ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (f"%{spid}%", MAX_TICKETS_PER_COMPLEX),
    ).fetchall()
    orders = conn.execute(
        "SELECT status FROM fibbee_orders WHERE sales_point_id = ? "
        "ORDER BY received_at DESC LIMIT ?",
        (spid, ORDERS_WINDOW),
    ).fetchall()
    total = len(orders)
    failed = sum(1 for o in orders if o["status"] == "failed")
    return {
        "sales_point_id": spid,
        "name": sp.get("name_ru") or sp.get("name_en") or spid,
        "status": sp.get("status"),
        "open_tickets": [
            {"node": t["node"], "zone": t["zone"], "priority": t["priority"], "description": t["description"]}
            for t in tickets
        ],
        "orders_total": total,
        "orders_failed": failed,
    }


def _prompt_for_batch(batch: list[dict]) -> list[dict]:
    lines = []
    for item in batch:
        tickets_txt = "; ".join(
            f"[{t['priority'] or '?'}/{t['node'] or '?'}] {(t['description'] or '').strip()}"
            for t in item["open_tickets"]
        ) or "нет открытых тикетов"
        fail_rate = (
            f"{item['orders_failed']}/{item['orders_total']}" if item["orders_total"] else "нет данных о заказах"
        )
        lines.append(
            f'id={item["sales_point_id"]} название="{item["name"]}" статус_комплекса={item["status"]}\n'
            f"  открытые тикеты: {tickets_txt}\n"
            f"  неудачные заказы (последние {ORDERS_WINDOW}): {fail_rate}"
        )
    user_content = "\n\n".join(lines)

    system = (
        "Ты аналитик техподдержки сети кофейных комплексов DrinkX. По каждому "
        "комплексу ниже дан список открытых тикетов (приоритет/узел/описание) и "
        "доля неудачных заказов за последние операции. Оцени уровень внимания:\n"
        "- critical: есть тикет с приоритетом urgent/high, или доля неудачных "
        "заказов очень высокая (заметно больше 20%), или несколько открытых "
        "тикетов одновременно.\n"
        "- warning: есть 1 открытый тикет средней/низкой важности, или заметная "
        "но не критичная доля неудачных заказов (примерно 5-20%).\n"
        "- ok: открытых тикетов нет и доля неудачных заказов низкая или данных "
        "недостаточно.\n\n"
        "Верни СТРОГО валидный JSON-массив без пояснений и без markdown-обёртки "
        "(без ```), по одному объекту на каждый комплекс из списка, в том же "
        "порядке id. reason — МАКСИМУМ 6 слов, коротко:\n"
        '[{"id": "...", "level": "ok"|"warning"|"critical", "reason": "макс. 6 слов"}]'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _parse_response(text: str) -> list[dict]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # Живьём поймано: при батче в 15 комплексов модель иногда упирается в
    # max_tokens и обрезает JSON-массив посреди объекта ("Unterminated
    # string"/"Expecting value") — вместо того чтобы ронять весь батч,
    # вытаскиваем отдельные валидные {...} объекты по одному (наши объекты
    # плоские, без вложенных скобок — regex на них безопасен) и отбрасываем
    # только оборванный хвост.
    items = []
    for match in re.finditer(r"\{[^{}]*\}", cleaned):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    if items:
        return items
    raise ValueError(f"не удалось распарсить JSON ни целиком, ни по объектам: {cleaned[:200]!r}")


def run_analysis(client, sales_points: list[dict], provider: str = "gemini_direct") -> dict:
    """Полный синхронный прогон анализа по переданным комплексам (игнор-лист
    уже должен быть применён вызывающим кодом — см. main.py). Батчит запросы
    к модели по BATCH_SIZE, сохраняет и возвращает результат."""
    conn = _connect()
    if not conn:
        payload = {
            "generated_at": None,
            "results": {},
            "errors": ["Fibbee БД не найдена — коннектор ещё не синхронизировался."],
        }
        return payload

    signals = [_gather_signal(conn, sp) for sp in sales_points if sp.get("sales_point_id")]
    conn.close()

    results: dict[str, dict] = {}
    errors: list[str] = []
    for i in range(0, len(signals), BATCH_SIZE):
        batch = signals[i : i + BATCH_SIZE]
        messages = _prompt_for_batch(batch)
        try:
            out = client.chat(
                messages, tier="flash", task_label="complex_analysis",
                provider=provider, retries=4, max_tokens=2500,
            )
            parsed = _parse_response(out["text"])
        except Exception as e:
            errors.append(f"батч {i // BATCH_SIZE + 1} ({len(batch)} комплексов): {e}")
            continue
        by_id = {str(item.get("id")): item for item in parsed if isinstance(item, dict)}
        for sig in batch:
            spid = sig["sales_point_id"]
            item = by_id.get(spid)
            if not item or item.get("level") not in LEVELS:
                results[spid] = {"level": "unknown", "reason": "модель не вернула валидный результат для этого id"}
            else:
                results[spid] = {"level": item["level"], "reason": item.get("reason", "")}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "errors": errors,
    }
    _save_cache(payload)
    return payload


def _save_cache(payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {"generated_at": None, "results": {}, "errors": []}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"generated_at": None, "results": {}, "errors": []}
