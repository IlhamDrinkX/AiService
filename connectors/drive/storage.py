"""
SQLite storage for the Drive connector. Same local-first pattern as the
other connectors (see connectors/discord/storage.py, connectors/gmail/storage.py).
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_files (
    file_id         TEXT PRIMARY KEY,
    drive_id        TEXT,       -- NULL = My Drive, else Shared Drive id
    drive_name      TEXT,
    name            TEXT,
    mime_type       TEXT,
    modified_time   TEXT,
    web_view_link   TEXT,
    parents         TEXT,       -- JSON list of parent folder ids
    size_bytes      INTEGER,
    text_content    TEXT,       -- extracted text, NULL if unsupported/not extracted
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drive_files_name ON drive_files (name);
CREATE INDEX IF NOT EXISTS idx_drive_files_drive ON drive_files (drive_id);
"""


class DriveStorage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_modified_time(self, file_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT modified_time FROM drive_files WHERE file_id = ?", (file_id,)
            ).fetchone()
            return row[0] if row else None

    def get_text(self, file_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT text_content FROM drive_files WHERE file_id = ?", (file_id,)
            ).fetchone()
            return row[0] if row else None

    def save_file(self, f: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO drive_files (
                    file_id, drive_id, drive_name, name, mime_type, modified_time,
                    web_view_link, parents, size_bytes, text_content, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f["file_id"],
                    f.get("drive_id"),
                    f.get("drive_name"),
                    f.get("name"),
                    f.get("mime_type"),
                    f.get("modified_time"),
                    f.get("web_view_link"),
                    json.dumps(f.get("parents", [])),
                    f.get("size_bytes", 0),
                    f.get("text_content"),
                    f["fetched_at"],
                ),
            )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM drive_files").fetchone()[0]

    def count_with_text(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM drive_files WHERE text_content IS NOT NULL"
            ).fetchone()[0]

    def reset_by_mime(self, mime_types: list[str]) -> int:
        """Обнулить modified_time у всех файлов заданных mime-типов —
        используется, когда логика извлечения текста для этого типа файла
        поменялась целиком (не обрезание, а сама извлечённая информация
        была неполной, напр. Sheets/Slides до 2026-07-19 читали только
        первый лист / только видимый текст без заметок). В отличие от
        reset_truncated() здесь неважна длина text_content — переизвлечь
        нужно ВСЕ файлы этих типов, т.к. раньше для них физически не было
        способа получить остальные данные через старый код."""
        if not mime_types:
            return 0
        placeholders = ",".join("?" for _ in mime_types)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE drive_files SET modified_time = NULL WHERE mime_type IN ({placeholders})",
                mime_types,
            )
            return cur.rowcount

    def reset_truncated(self, old_cap_chars: int) -> int:
        """Обнулить modified_time у файлов, чей text_content по длине ровно
        совпадает со старым TEXT_MAX_CHARS — это почти наверняка след
        старого обрезания (реальный документ такой длины символ-в-символ —
        статистически невероятное совпадение), а не совпадение. sync.py
        сравнивает modified_time, чтобы решить, перечитывать файл или нет;
        обнулив его здесь, следующий запуск sync.py гарантированно
        перекачает и переизвлечёт текст этих файлов заново — уже без
        обрезания (см. TEXT_MAX_CHARS=0 в .env)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE drive_files SET modified_time = NULL WHERE LENGTH(text_content) = ?",
                (old_cap_chars,),
            )
            return cur.rowcount
