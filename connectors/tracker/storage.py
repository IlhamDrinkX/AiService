"""
SQLite storage for the DrinkX Tracker connector.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracker_boards (
    code         TEXT PRIMARY KEY,   -- e.g. DEV, TEST, CAD
    board_id     INTEGER,
    name         TEXT,
    order_index  INTEGER,
    statuses     TEXT,               -- JSON list of {id, name, isInitial, isFinal, isInProgress, ...}
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracker_tasks (
    task_id           TEXT PRIMARY KEY,  -- uuid
    code              TEXT,              -- e.g. DEV-478
    task_type_code    TEXT,
    sequence_number   INTEGER,
    title             TEXT,
    description       TEXT,
    status_id         TEXT,
    status_name       TEXT,              -- resolved from tracker_boards.statuses for convenience
    status_changed_at TEXT,
    is_urgent         INTEGER,
    assignee_name     TEXT,
    assignee_email    TEXT,
    reporter_name     TEXT,
    reporter_email    TEXT,
    tags              TEXT,              -- JSON list
    subtask_count     INTEGER,
    parent_code       TEXT,
    last_activity_at  TEXT,
    created_at        TEXT,
    updated_at        TEXT,
    fetched_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracker_tasks_board ON tracker_tasks (task_type_code);
CREATE INDEX IF NOT EXISTS idx_tracker_tasks_status ON tracker_tasks (status_id);
"""


class TrackerStorage:
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

    def save_board(self, board: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tracker_boards (
                    code, board_id, name, order_index, statuses, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    board["code"],
                    board["id"],
                    board["name"],
                    board.get("orderIndex"),
                    json.dumps(board.get("statuses", [])),
                    board["fetched_at"],
                ),
            )

    def save_task(self, t: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tracker_tasks (
                    task_id, code, task_type_code, sequence_number, title, description,
                    status_id, status_name, status_changed_at, is_urgent,
                    assignee_name, assignee_email, reporter_name, reporter_email,
                    tags, subtask_count, parent_code, last_activity_at,
                    created_at, updated_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t["task_id"],
                    t.get("code"),
                    t.get("task_type_code"),
                    t.get("sequence_number"),
                    t.get("title"),
                    t.get("description"),
                    t.get("status_id"),
                    t.get("status_name"),
                    t.get("status_changed_at"),
                    1 if t.get("is_urgent") else 0,
                    t.get("assignee_name"),
                    t.get("assignee_email"),
                    t.get("reporter_name"),
                    t.get("reporter_email"),
                    json.dumps(t.get("tags", [])),
                    t.get("subtask_count", 0),
                    t.get("parent_code"),
                    t.get("last_activity_at"),
                    t.get("created_at"),
                    t.get("updated_at"),
                    t["fetched_at"],
                ),
            )

    def count_tasks(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM tracker_tasks").fetchone()[0]

    def count_boards(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM tracker_boards").fetchone()[0]
