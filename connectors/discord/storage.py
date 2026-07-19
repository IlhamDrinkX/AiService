"""
SQLite storage for the Discord connector.

Schema is intentionally close to what the shared PostgreSQL schema will look
like later (see architecture_plan.md), so migrating is a straight table copy:
guild_id, channel_id, author_id are stored as TEXT (Discord snowflakes can
exceed 53-bit safe integer range in some languages, TEXT avoids any doubt).

Covers not just top-level text-channel messages but also thread messages
(regular threads + forum posts) and voice-channel text chat, plus edit
history and soft deletes, so the record kept here reflects the full life of
a message, not just its first snapshot.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_messages (
    message_id           TEXT PRIMARY KEY,
    guild_id              TEXT,
    guild_name             TEXT,
    channel_id            TEXT NOT NULL,
    channel_name           TEXT,
    channel_type           TEXT,     -- text | voice | thread | forum_post
    parent_channel_id      TEXT,     -- set for threads/forum posts: the channel they live under
    author_id              TEXT,
    author_name             TEXT,
    content                TEXT,
    attachments             TEXT,      -- JSON list of attachment URLs
    reply_to_message_id     TEXT,     -- set if this message is a reply
    created_at              TEXT NOT NULL,   -- ISO 8601, message timestamp in Discord
    edited_at               TEXT,     -- ISO 8601, timestamp of the most recent known edit
    deleted_at               TEXT,     -- ISO 8601, when we learned it was deleted (content kept)
    fetched_at               TEXT NOT NULL    -- ISO 8601, when we first stored it
);

CREATE INDEX IF NOT EXISTS idx_discord_messages_channel
    ON discord_messages (channel_id, created_at);

CREATE INDEX IF NOT EXISTS idx_discord_messages_parent_channel
    ON discord_messages (parent_channel_id);

-- One row per known edit, so the pre-edit content isn't lost when content
-- gets overwritten on the main row.
CREATE TABLE IF NOT EXISTS discord_message_edits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT NOT NULL,
    old_content     TEXT,     -- NULL if we didn't have the pre-edit content cached
    edited_at       TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES discord_messages (message_id)
);

CREATE INDEX IF NOT EXISTS idx_discord_message_edits_message
    ON discord_message_edits (message_id);
"""

# Columns added after the original release. Kept as (name, ddl_type) so
# existing SQLite files get migrated in place instead of requiring a wipe.
_MIGRATION_COLUMNS = [
    ("channel_type", "TEXT"),
    ("parent_channel_id", "TEXT"),
    ("reply_to_message_id", "TEXT"),
    ("edited_at", "TEXT"),
    ("deleted_at", "TEXT"),
]


class DiscordStorage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(discord_messages)")}
        for name, ddl_type in _MIGRATION_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE discord_messages ADD COLUMN {name} {ddl_type}")

    def save_message(self, msg: dict) -> None:
        """Insert a message; ignore if message_id already exists (idempotent).

        For updating an existing message (edits), use record_edit() instead —
        this only inserts, it never overwrites.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO discord_messages (
                    message_id, guild_id, guild_name, channel_id, channel_name,
                    channel_type, parent_channel_id, author_id, author_name,
                    content, attachments, reply_to_message_id, created_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg["message_id"],
                    msg.get("guild_id"),
                    msg.get("guild_name"),
                    msg["channel_id"],
                    msg.get("channel_name"),
                    msg.get("channel_type"),
                    msg.get("parent_channel_id"),
                    msg.get("author_id"),
                    msg.get("author_name"),
                    msg.get("content"),
                    json.dumps(msg.get("attachments", [])),
                    msg.get("reply_to_message_id"),
                    msg["created_at"],
                    msg["fetched_at"],
                ),
            )

    def record_edit(self, message_id: str, new_content: str, old_content: str | None, edited_at: str) -> None:
        """Log an edit and update the main row's content to the latest version.

        If the message_id isn't known yet (e.g. we started listening after it
        was originally posted), this still records the edit event but there's
        no base row to update content on — save_message() should be called
        first when possible.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO discord_message_edits (message_id, old_content, edited_at) VALUES (?, ?, ?)",
                (message_id, old_content, edited_at),
            )
            conn.execute(
                "UPDATE discord_messages SET content = ?, edited_at = ? WHERE message_id = ?",
                (new_content, edited_at, message_id),
            )

    def record_delete(self, message_id: str, deleted_at: str) -> None:
        """Mark a message as deleted without erasing its stored content."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE discord_messages SET deleted_at = ? WHERE message_id = ?",
                (deleted_at, message_id),
            )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM discord_messages").fetchone()[0]
