"""
SQLite storage for the Gmail connector.

Mirrors the approach used in connectors/discord/storage.py: local SQLite now,
straight table copy into the shared PostgreSQL schema later. A small
`sync_state` table tracks Gmail's historyId so re-runs are incremental
instead of re-scanning the whole mailbox.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_messages (
    message_id       TEXT PRIMARY KEY,
    thread_id         TEXT,
    sender            TEXT,
    to_recipients     TEXT,
    cc_recipients     TEXT,
    subject           TEXT,
    date              TEXT,        -- ISO 8601
    snippet           TEXT,
    body_text         TEXT,
    labels            TEXT,        -- JSON list
    has_attachments   INTEGER NOT NULL DEFAULT 0,
    attachments       TEXT,        -- JSON list of {filename, attachment_id, mime_type}
    fetched_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gmail_messages_date ON gmail_messages (date);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class GmailStorage:
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

    def save_message(self, msg: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO gmail_messages (
                    message_id, thread_id, sender, to_recipients, cc_recipients,
                    subject, date, snippet, body_text, labels,
                    has_attachments, attachments, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg["message_id"],
                    msg.get("thread_id"),
                    msg.get("sender"),
                    msg.get("to_recipients"),
                    msg.get("cc_recipients"),
                    msg.get("subject"),
                    msg.get("date"),
                    msg.get("snippet"),
                    msg.get("body_text"),
                    json.dumps(msg.get("labels", [])),
                    1 if msg.get("attachments") else 0,
                    json.dumps(msg.get("attachments", [])),
                    msg["fetched_at"],
                ),
            )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM gmail_messages").fetchone()[0]

    def get_history_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'history_id'"
            ).fetchone()
            return row[0] if row else None

    def set_history_id(self, history_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('history_id', ?)",
                (history_id,),
            )
