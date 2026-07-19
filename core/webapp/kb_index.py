"""
Индексация базы знаний (Google Drive + Discord) в локальный SQLite-кэш
эмбеддингов через OpenRouter (модуль 2 из functional_plan_ui.md).

Без Postgres/pgvector — на объёме этого проекта (634 файла Drive с текстом
~2.2М токенов, Discord — сотни тысяч токенов за разумный период) брутфорс
косинус в numpy по кэшированным векторам, загруженным в память при запросе,
на порядок проще в установке на одном ноутбуке и достаточно быстр. Если
объём вырастет на порядок — тогда есть смысл переезжать на pgvector (см.
functional_plan_ui.md, §5); переезд не потребует менять сам эмбеддинг —
модель зафиксирована в core/router_ai/models.yaml.

Пересчёт эмбеддинга — только если текст чанка изменился (кэш по хэшу
содержимого), см. functional_plan_ui.md §4.3.

Запуск (venv — тот же, что у core/router_ai + numpy, см. README):
    python kb_index.py build                       # Drive (весь текст) + Discord (90 дней)
    python kb_index.py build --drive-only
    python kb_index.py build --discord-only --discord-days 30
    python kb_index.py stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router_ai"))
from client import RouterAIClient, RouterAIError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DRIVE_DB = ROOT / "connectors" / "drive" / "data" / "drive.db"
DISCORD_DB = ROOT / "connectors" / "discord" / "data" / "discord.db"
INDEX_DB = Path(__file__).resolve().parent / "data" / "kb_index.db"

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 150
EMBED_BATCH = 32  # меньше 64 после реального прогона — крупные батчи чаще ловили таймаут

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,        -- 'drive' | 'discord'
    ref TEXT NOT NULL,           -- имя файла / "канал, дата"
    title TEXT,
    url TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    content_hash TEXT UNIQUE NOT NULL,
    embedding TEXT NOT NULL,     -- JSON-массив float
    indexed_at TEXT NOT NULL
);
"""


def _init_db() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_DB)
    conn.execute(SCHEMA)
    return conn


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def _existing_hashes(conn: sqlite3.Connection) -> set:
    return {row[0] for row in conn.execute("SELECT content_hash FROM kb_chunks")}


def _embed_and_store(conn: sqlite3.Connection, client: RouterAIClient, pending: list[dict]):
    """pending: [{"source","ref","title","url","chunk_text","content_hash"}]"""
    for i in range(0, len(pending), EMBED_BATCH):
        batch = pending[i : i + EMBED_BATCH]
        vectors = client.embed([p["chunk_text"] for p in batch], task_label="kb_index")
        now = datetime.now(timezone.utc).isoformat()
        for p, vec in zip(batch, vectors):
            conn.execute(
                "INSERT OR IGNORE INTO kb_chunks (source, ref, title, url, chunk_text, content_hash, embedding, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (p["source"], p["ref"], p["title"], p["url"], p["chunk_text"], p["content_hash"], json.dumps(vec), now),
            )
        conn.commit()
        print(f"  ...{min(i + EMBED_BATCH, len(pending))}/{len(pending)}")


def build_drive(conn: sqlite3.Connection, client: RouterAIClient):
    if not DRIVE_DB.exists():
        print("Drive: база не найдена, пропуск")
        return
    src = sqlite3.connect(DRIVE_DB)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT name, web_view_link, text_content FROM drive_files "
        "WHERE text_content IS NOT NULL AND text_content != ''"
    ).fetchall()
    src.close()

    existing = _existing_hashes(conn)
    pending = []
    for r in rows:
        for chunk in _chunk_text(r["text_content"]):
            h = _hash(chunk)
            if h in existing:
                continue
            pending.append(
                {
                    "source": "drive",
                    "ref": r["name"],
                    "title": r["name"],
                    "url": r["web_view_link"] or "",
                    "chunk_text": chunk,
                    "content_hash": h,
                }
            )
    print(f"Drive: {len(rows)} файлов, {len(pending)} новых/изменённых чанков к индексации")
    if pending:
        _embed_and_store(conn, client, pending)


def build_discord(conn: sqlite3.Connection, client: RouterAIClient, days: int):
    if not DISCORD_DB.exists():
        print("Discord: база не найдена, пропуск")
        return
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    src = sqlite3.connect(DISCORD_DB)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT guild_id, channel_id, channel_name, author_name, content, created_at "
        "FROM discord_messages WHERE created_at >= ? AND deleted_at IS NULL AND content != '' "
        "ORDER BY channel_id, created_at",
        (since,),
    ).fetchall()
    src.close()

    # группировка: канал + календарный день (UTC) -> один транскрипт
    groups: dict[tuple, list] = {}
    for r in rows:
        day = r["created_at"][:10]
        key = (r["channel_id"], r["channel_name"], day)
        groups.setdefault(key, []).append(r)

    existing = _existing_hashes(conn)
    pending = []
    for (channel_id, channel_name, day), msgs in groups.items():
        transcript = "\n".join(f"{m['author_name']}: {m['content']}" for m in msgs)
        guild_id = msgs[0]["guild_id"]
        # ссылка ведёт на первое сообщение дня в канале — Discord сам
        # прокрутит контекст рядом при переходе
        url = f"https://discord.com/channels/{guild_id}/{channel_id}"
        ref = f"#{channel_name}, {day}"
        for chunk in _chunk_text(transcript):
            h = _hash(chunk)
            if h in existing:
                continue
            pending.append(
                {
                    "source": "discord",
                    "ref": ref,
                    "title": ref,
                    "url": url,
                    "chunk_text": chunk,
                    "content_hash": h,
                }
            )
    print(f"Discord: {len(rows)} сообщений за {days} дн., {len(groups)} день-канал групп, {len(pending)} новых чанков")
    if pending:
        _embed_and_store(conn, client, pending)


def search(query: str, client: RouterAIClient, top_k: int = 6) -> list[dict]:
    """
    Брутфорс косинус по всем чанкам в памяти — см. пояснение про масштаб в
    докстринге модуля. Возвращает top_k чанков как items для
    RouterAIClient.report() (поля source/ref/title/url/text/status).
    """
    import numpy as np

    if not INDEX_DB.exists():
        return []
    conn = sqlite3.connect(INDEX_DB)
    rows = conn.execute("SELECT source, ref, title, url, chunk_text, embedding FROM kb_chunks").fetchall()
    conn.close()
    if not rows:
        return []

    matrix = np.array([json.loads(r[5]) for r in rows], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1e-9
    matrix_normed = matrix / norms

    q_vec = np.array(client.embed([query], task_label="kb_search")[0], dtype=np.float32)
    q_normed = q_vec / (np.linalg.norm(q_vec) or 1e-9)

    scores = matrix_normed @ q_normed
    top_idx = np.argsort(-scores)[:top_k]

    results = []
    for idx in top_idx:
        source, ref, title, url, chunk_text, _ = rows[idx]
        results.append(
            {
                "source": "Google Drive" if source == "drive" else "Discord",
                "ref": ref,
                "title": title,
                "status": f"score={scores[idx]:.2f}",
                "url": url,
                "text": chunk_text,
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser(description="Индексация базы знаний (Drive + Discord)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build")
    build.add_argument("--drive-only", action="store_true")
    build.add_argument("--discord-only", action="store_true")
    build.add_argument("--discord-days", type=int, default=90)

    sub.add_parser("stats")

    args = parser.parse_args()
    conn = _init_db()

    if args.cmd == "stats":
        total = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        by_source = conn.execute("SELECT source, COUNT(*) FROM kb_chunks GROUP BY source").fetchall()
        print(f"Всего чанков: {total}")
        for source, count in by_source:
            print(f"  {source}: {count}")
        return

    try:
        client = RouterAIClient()
    except RouterAIError as e:
        print(f"Ошибка конфигурации router_ai: {e}")
        sys.exit(1)

    if not args.discord_only:
        build_drive(conn, client)
    if not args.drive_only:
        build_discord(conn, client, args.discord_days)

    conn.close()
    print("Готово.")


if __name__ == "__main__":
    main()
