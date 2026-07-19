"""
SQLite-хранилище для Service Desk коннектора.

Вложения (files[].dataUrl) — это base64 прямо в JSON тикета (фото, акты,
PDF). Хранить их в SQLite как текст — плохая идея (база быстро раздуется и
станет неудобной для запросов), поэтому тут они декодируются на диск в
FILES_DIR, а в таблицу пишется только путь.
"""

import base64
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS servicedesk_tickets (
    -- TEXT, не INTEGER: в конце 2026 API стало отдавать строковые id вида
    -- "ticket-19f7a7a5c1c-63bd43" вместо чисел — старая схема (INTEGER
    -- PRIMARY KEY) роняла sqlite3.IntegrityError: datatype mismatch на
    -- КАЖДОЙ заявке. Пойман 2026-07-19 при первом реальном прогоне sync.py
    -- в автопланировщике (core/scheduler/run.py).
    ticket_id           TEXT PRIMARY KEY,
    code                TEXT,              -- SD-1025
    node                TEXT,              -- тип оборудования: ККТ, Кофемашина, ...
    title               TEXT,
    client              TEXT,
    object              TEXT,              -- название объекта/точки
    status              TEXT,
    closed_at           TEXT,
    engineer            TEXT,
    priority            TEXT,
    severity            TEXT,
    warranty            TEXT,
    work_done           TEXT,
    act_number          TEXT,
    complexes           TEXT,              -- JSON list
    created_at          TEXT,
    created_by          TEXT,
    diagnosis           TEXT,
    l1_actions          TEXT,
    materials           TEXT,
    problem_id          TEXT,
    incident_at         TEXT,
    work_result         TEXT,
    description         TEXT,
    failure_reason      TEXT,
    related_tickets     TEXT,              -- JSON list
    engineer_comment    TEXT,
    paper_act_received  INTEGER,
    has_signed_act_photo INTEGER,
    fetched_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS servicedesk_ticket_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT NOT NULL,
    at         TEXT,
    actor      TEXT,
    event      TEXT,
    diff       TEXT,                       -- JSON
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES servicedesk_tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS servicedesk_ticket_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT NOT NULL,
    name       TEXT,
    size       INTEGER,
    type       TEXT,
    file_path  TEXT,                       -- относительный путь на диске, не base64
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES servicedesk_tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS servicedesk_clients (
    client_id        TEXT PRIMARY KEY,
    legal_name       TEXT,
    brand_name       TEXT,
    inn              TEXT,
    contract_number  TEXT,
    contract_date    TEXT,
    contract_until   TEXT,
    drinkx_entity    TEXT,
    contacts         TEXT,                 -- JSON
    objects_count    INTEGER,
    complexes_count  INTEGER,
    comment          TEXT,
    fetched_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sd_tickets_status ON servicedesk_tickets (status);
CREATE INDEX IF NOT EXISTS idx_sd_tickets_client ON servicedesk_tickets (client);
CREATE INDEX IF NOT EXISTS idx_sd_audit_ticket ON servicedesk_ticket_audit (ticket_id);
CREATE INDEX IF NOT EXISTS idx_sd_files_ticket ON servicedesk_ticket_files (ticket_id);
"""

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9А-Яа-яЁё._-]+")


def _safe_name(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", name).strip("_") or "file"


class ServiceDeskStorage:
    def __init__(self, db_path: str, files_dir: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.files_dir = Path(files_dir)
        self.files_dir.mkdir(parents=True, exist_ok=True)
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

    def save_ticket(self, t: dict, fetched_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO servicedesk_tickets (
                    ticket_id, code, node, title, client, object, status, closed_at,
                    engineer, priority, severity, warranty, work_done, act_number,
                    complexes, created_at, created_by, diagnosis, l1_actions, materials,
                    problem_id, incident_at, work_result, description, failure_reason,
                    related_tickets, engineer_comment, paper_act_received,
                    has_signed_act_photo, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t["id"],
                    t.get("code"),
                    t.get("node"),
                    t.get("title"),
                    t.get("client"),
                    t.get("object"),
                    t.get("status"),
                    t.get("closedAt"),
                    t.get("engineer"),
                    t.get("priority"),
                    t.get("severity"),
                    t.get("warranty"),
                    t.get("workDone"),
                    t.get("actNumber"),
                    json.dumps(t.get("complexes", []), ensure_ascii=False),
                    t.get("createdAt"),
                    t.get("createdBy"),
                    t.get("diagnosis"),
                    t.get("l1Actions"),
                    t.get("materials"),
                    t.get("problemId"),
                    t.get("incidentAt"),
                    t.get("workResult"),
                    t.get("description"),
                    t.get("failureReason"),
                    json.dumps(t.get("relatedTickets", []), ensure_ascii=False),
                    t.get("engineerComment"),
                    1 if t.get("paperActReceived") else 0,
                    1 if t.get("hasSignedActPhoto") else 0,
                    fetched_at,
                ),
            )

            conn.execute("DELETE FROM servicedesk_ticket_audit WHERE ticket_id = ?", (t["id"],))
            for entry in t.get("audit", []):
                conn.execute(
                    """
                    INSERT INTO servicedesk_ticket_audit (ticket_id, at, actor, event, diff, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t["id"],
                        entry.get("at"),
                        entry.get("actor"),
                        entry.get("event"),
                        json.dumps(entry.get("diff"), ensure_ascii=False),
                        fetched_at,
                    ),
                )

            conn.execute("DELETE FROM servicedesk_ticket_files WHERE ticket_id = ?", (t["id"],))
            for f in t.get("files", []):
                file_path = self._save_file_to_disk(t.get("code") or str(t["id"]), f)
                conn.execute(
                    """
                    INSERT INTO servicedesk_ticket_files (ticket_id, name, size, type, file_path, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (t["id"], f.get("name"), f.get("size"), f.get("type"), file_path, fetched_at),
                )

    def _save_file_to_disk(self, ticket_code: str, f: dict) -> str | None:
        data_url = f.get("dataUrl")
        if not data_url or "," not in data_url:
            return None
        ticket_dir = self.files_dir / _safe_name(ticket_code)
        ticket_dir.mkdir(parents=True, exist_ok=True)
        out_path = ticket_dir / _safe_name(f.get("name") or "file")
        try:
            _, b64_payload = data_url.split(",", 1)
            out_path.write_bytes(base64.b64decode(b64_payload))
        except (ValueError, base64.binascii.Error):
            return None
        return str(out_path)

    def save_client(self, c: dict, fetched_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO servicedesk_clients (
                    client_id, legal_name, brand_name, inn, contract_number, contract_date,
                    contract_until, drinkx_entity, contacts, objects_count, complexes_count,
                    comment, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(c["id"]),
                    c.get("legalName"),
                    c.get("brandName"),
                    c.get("inn"),
                    c.get("contractNumber"),
                    c.get("contractDate"),
                    c.get("contractUntil"),
                    c.get("drinkxEntity"),
                    json.dumps(c.get("contacts"), ensure_ascii=False),
                    c.get("objectsCount"),
                    c.get("complexesCount"),
                    c.get("comment"),
                    fetched_at,
                ),
            )

    def count_tickets(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM servicedesk_tickets").fetchone()[0]

    def count_files(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM servicedesk_ticket_files").fetchone()[0]
