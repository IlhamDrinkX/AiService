"""
Общий доступ к данным для веб-морды: читает SQLite коннекторов напрямую
(тот же приём, что в core/reporting/generate.py — не импортирует сами
коннекторы, у них несовместимые между собой зависимости в отдельных venv).

Загрузчики заявок/тикетов/задач переиспользуются из core/reporting/generate.py
(не дублируем SQL-запросы) — здесь добавлены только Discord/Drive/Fibbee-мониторинг,
которых там не было.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT / "core" / "reporting"))
import generate as reporting  # noqa: E402  (переиспользуем _load_* и LINK_TEMPLATES)

DISCORD_DB = ROOT / "connectors" / "discord" / "data" / "discord.db"
DRIVE_DB = ROOT / "connectors" / "drive" / "data" / "drive.db"
FIBBEE_DB = ROOT / "connectors" / "fibbee" / "data" / "fibbee.db"


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------- #
# Задачи (модуль 1) — источник = оперативные (SD+Fibbee) / стратегические (Tracker)
# ---------------------------------------------------------------------- #

def load_all_tasks(since=None, limit=500) -> dict:
    operational = reporting._load_servicedesk(since, limit) + reporting._load_fibbee(since, limit)
    strategic = reporting._load_tracker(since, limit)
    for it in operational:
        it["group"] = "Оперативные"
    for it in strategic:
        it["group"] = "Стратегические"
    items = operational + strategic
    items.sort(key=lambda it: it.get("created_at") or "", reverse=True)
    return {"operational": operational, "strategic": strategic, "all": items}


def load_recently_updated(limit: int = 30) -> list[dict]:
    """
    'Обновления' в v1 — не полноценный diff-слой (нужен снапшот истории
    прогонов sync.py, ещё не собран), а просто сортировка по updated_at
    там, где это поле есть. Честная замена, пока не появится task_updates.
    """
    items = []
    conn = _connect(ROOT / "connectors" / "servicedesk" / "data" / "servicedesk.db")
    if conn:
        for r in conn.execute(
            "SELECT code, title, status, closed_at, created_at FROM servicedesk_tickets "
            "ORDER BY COALESCE(closed_at, created_at) DESC LIMIT ?",
            (limit,),
        ):
            items.append(
                {
                    "source": "Service Desk",
                    "ref": r["code"],
                    "title": r["title"],
                    "status": r["status"],
                    "at": r["closed_at"] or r["created_at"],
                    "url": reporting.LINK_TEMPLATES.get("servicedesk", "").format(code=r["code"] or ""),
                }
            )
        conn.close()

    conn = _connect(ROOT / "connectors" / "fibbee" / "data" / "fibbee.db")
    if conn:
        for r in conn.execute(
            "SELECT ticket_id, number, status, state, updated_at FROM fibbee_tickets "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ):
            items.append(
                {
                    "source": "Fibbee ERP",
                    "ref": f"#{r['number']}" if r["number"] is not None else r["ticket_id"],
                    "title": f"{r['status']} → {r['state']}",
                    "status": r["state"],
                    "at": r["updated_at"],
                    "url": reporting.LINK_TEMPLATES.get("fibbee", "").format(
                        ticket_id=r["ticket_id"], number=r["number"] or ""
                    ),
                }
            )
        conn.close()

    conn = _connect(ROOT / "connectors" / "tracker" / "data" / "tracker.db")
    if conn:
        for r in conn.execute(
            "SELECT code, task_type_code, title, status_name, last_activity_at FROM tracker_tasks "
            "ORDER BY last_activity_at DESC LIMIT ?",
            (limit,),
        ):
            items.append(
                {
                    "source": "Tracker",
                    "ref": r["code"],
                    "title": r["title"],
                    "status": r["status_name"],
                    "at": r["last_activity_at"],
                    "url": reporting.LINK_TEMPLATES.get("tracker", "").format(
                        code=r["code"] or "", type=r["task_type_code"] or ""
                    ),
                }
            )
        conn.close()

    items.sort(key=lambda it: it.get("at") or "", reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------- #
# Discord (модуль 3)
# ---------------------------------------------------------------------- #

def discord_channel_url(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def load_discord_messages(since_iso: str, until_iso: str, channel: str | None = None, limit: int = 500) -> list[dict]:
    conn = _connect(DISCORD_DB)
    if not conn:
        return []
    query = (
        "SELECT message_id, guild_id, channel_id, channel_name, author_name, content, created_at "
        "FROM discord_messages WHERE created_at >= ? AND created_at <= ? AND deleted_at IS NULL"
    )
    params = [since_iso, until_iso]
    if channel:
        query += " AND channel_name = ?"
        params.append(channel)
    query += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    # разворачиваем сырые <@ID>-упоминания в читаемые имена и приводим время
    # к нормальному формату — по просьбе пользователя (2026-07-19), раньше
    # показывались голый Discord ID и полный ISO-таймстамп с микросекундами.
    author_map = reporting.discord_author_map()
    messages = []
    for r in rows:
        m = dict(r)
        m["content"] = reporting.resolve_discord_mentions(m["content"], author_map)
        m["created_at"] = reporting.format_discord_dt(m["created_at"])
        messages.append(m)
    return messages


def list_discord_channels() -> list[str]:
    conn = _connect(DISCORD_DB)
    if not conn:
        return []
    rows = conn.execute(
        "SELECT channel_name, COUNT(*) c FROM discord_messages GROUP BY channel_name ORDER BY c DESC LIMIT 60"
    ).fetchall()
    conn.close()
    return [r["channel_name"] for r in rows if r["channel_name"]]


# ---------------------------------------------------------------------- #
# Мониторинг комплексов (модуль 3)
# ---------------------------------------------------------------------- #

def load_sales_points() -> list[dict]:
    conn = _connect(FIBBEE_DB)
    if not conn:
        return []
    names = [r[1] for r in conn.execute("PRAGMA table_info(fibbee_sales_points)")]
    if not names:
        conn.close()
        return []
    rows = conn.execute("SELECT * FROM fibbee_sales_points").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_healthchecks() -> list[dict]:
    conn = _connect(FIBBEE_DB)
    if not conn:
        return []
    names = [r[1] for r in conn.execute("PRAGMA table_info(fibbee_healthchecks)")]
    if not names:
        conn.close()
        return []
    rows = conn.execute("SELECT * FROM fibbee_healthchecks ORDER BY fetched_at DESC LIMIT 500").fetchall()
    conn.close()
    return [dict(r) for r in rows]
