"""
SQLite-хранилище для Fibbee ERP коннектора.

Каждый salesPoint в ответе /v1/sales-points/list — очень большой объект:
кроме операционных полей (статус, локация, мойки, журнал передачи смены)
там огромный `osconfig` (провижининг конкретной кофемашины/киоска: прайс-
листы, экраны, рабочие часы устройства и т.п.) — это, по сути, конфиг
железа, а не телеметрия/статус для мониторинга.

Точную вложенность части полей (cleanings, handoverJournal) видели только
в одном примере ответа, поэтому решение: вытащить в колонки то, в чём
уверены (id, статус, локация, франчайзи, склад, даты), а весь объект
целиком сохранить как raw_json — если понадобится что-то ещё (например,
реально разбирать osconfig), не нужно будет менять схему, данные уже на
диске.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fibbee_sales_points (
    sales_point_id      TEXT PRIMARY KEY,
    name_ru             TEXT,
    name_en             TEXT,
    brand               TEXT,
    status              TEXT,              -- production/manufacturing/archived/discontinued
    city_id             TEXT,
    country_code        TEXT,
    timezone            TEXT,
    location_lat         REAL,
    location_lng         REAL,
    location_address     TEXT,
    franchisee_id       TEXT,
    warehouse_id        TEXT,
    payment_provider_id TEXT,
    no_remote           INTEGER,
    changed_by          TEXT,
    cleaning_info        TEXT,              -- JSON, точная форма не зафиксирована
    cleanings            TEXT,              -- JSON list (Big Wash/Quick Wash история)
    handover_message     TEXT,              -- последняя запись в журнале передачи смены
    handover_updated_at   TEXT,
    created_at          TEXT,
    updated_at          TEXT,
    raw_json            TEXT NOT NULL,      -- полный объект как пришёл от API
    fetched_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fibbee_sp_status ON fibbee_sales_points (status);
CREATE INDEX IF NOT EXISTS idx_fibbee_sp_franchisee ON fibbee_sales_points (franchisee_id);

-- Заказы. product_dump — сырая телеметрия варки конкретного напитка
-- (nozzle/milkTemp/waterQnty/cakePressFinal/drinkWeight/extractTime и т.п.)
-- это и есть "лог заказа" для последующего анализа.
CREATE TABLE IF NOT EXISTS fibbee_orders (
    order_id            TEXT PRIMARY KEY,
    sales_point_id      TEXT,
    number              INTEGER,
    status              TEXT,
    menu_item_id        TEXT,
    menu_item_name_ru   TEXT,
    menu_item_name_en   TEXT,
    total_sum           REAL,
    terminal            TEXT,
    kiosk_id            TEXT,
    user_id             TEXT,
    franchisee_id       TEXT,
    received_at         TEXT,
    brewed_at           TEXT,
    completed_at        TEXT,
    product_dump        TEXT,              -- JSON, лог заказа
    raw_json            TEXT NOT NULL,
    fetched_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fibbee_orders_sp ON fibbee_orders (sales_point_id);
CREATE INDEX IF NOT EXISTS idx_fibbee_orders_received ON fibbee_orders (received_at);
CREATE INDEX IF NOT EXISTS idx_fibbee_orders_status ON fibbee_orders (status);

-- Тикеты раздела "Сервис" (технические инциденты, дублируются в Discord).
CREATE TABLE IF NOT EXISTS fibbee_tickets (
    ticket_id           TEXT PRIMARY KEY,
    number              INTEGER,
    node                TEXT,
    zone                TEXT,
    status              TEXT,
    state               TEXT,
    category            TEXT,
    priority            TEXT,
    source              TEXT,
    description         TEXT,
    sales_point_ids     TEXT,              -- JSON list
    incident_time       TEXT,
    incident_end_time   TEXT,
    discord_link        TEXT,
    created_at          TEXT,
    created_by          TEXT,
    changed_by          TEXT,
    updated_at          TEXT,
    raw_json            TEXT NOT NULL,
    fetched_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fibbee_tickets_status ON fibbee_tickets (status);
CREATE INDEX IF NOT EXISTS idx_fibbee_tickets_updated ON fibbee_tickets (updated_at);

-- Общий аудит-лог изменений сущностей (changes-log/list).
CREATE TABLE IF NOT EXISTS fibbee_changes_log (
    change_id           TEXT PRIMARY KEY,
    type                TEXT,
    object_id           TEXT,
    changed_by           TEXT,
    updated_at           TEXT,
    changes_json         TEXT,
    raw_json             TEXT NOT NULL,
    fetched_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fibbee_changes_updated ON fibbee_changes_log (updated_at);

-- Остатки по менюайтемам на устройствах точки (healthchecks/list), снимок на момент синхронизации.
CREATE TABLE IF NOT EXISTS fibbee_healthchecks (
    sales_point_id       TEXT PRIMARY KEY,
    raw_json             TEXT NOT NULL,
    fetched_at           TEXT NOT NULL
);
"""


class FibbeeStorage:
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

    def save_sales_point(self, sp: dict, fetched_at: str) -> None:
        name = sp.get("name") or {}
        location = sp.get("location") or {}
        handover = sp.get("handoverJournal") or {}

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fibbee_sales_points (
                    sales_point_id, name_ru, name_en, brand, status, city_id,
                    country_code, timezone, location_lat, location_lng, location_address,
                    franchisee_id, warehouse_id, payment_provider_id, no_remote, changed_by,
                    cleaning_info, cleanings, handover_message, handover_updated_at,
                    created_at, updated_at, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sp.get("salesPointId"),
                    name.get("ru"),
                    name.get("en"),
                    sp.get("brand"),
                    sp.get("status"),
                    sp.get("cityId"),
                    sp.get("countryCode"),
                    sp.get("timezone"),
                    location.get("lat"),
                    location.get("lng"),
                    location.get("address"),
                    sp.get("franchiseeId"),
                    sp.get("warehouseId"),
                    sp.get("paymentProviderId"),
                    1 if sp.get("noRemote") else 0,
                    sp.get("changedBy"),
                    json.dumps(sp.get("cleaningInfo"), ensure_ascii=False),
                    json.dumps(sp.get("cleanings"), ensure_ascii=False),
                    handover.get("message"),
                    handover.get("updatedAt"),
                    sp.get("createdAt"),
                    sp.get("updatedAt"),
                    json.dumps(sp, ensure_ascii=False),
                    fetched_at,
                ),
            )

    def count_sales_points(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM fibbee_sales_points").fetchone()[0]

    # ---- заказы --------------------------------------------------------------

    def save_order(self, order: dict, fetched_at: str) -> None:
        menu_item = order.get("menuItem") or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fibbee_orders (
                    order_id, sales_point_id, number, status, menu_item_id,
                    menu_item_name_ru, menu_item_name_en, total_sum, terminal,
                    kiosk_id, user_id, franchisee_id, received_at, brewed_at,
                    completed_at, product_dump, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.get("orderId"),
                    order.get("salesPointId"),
                    order.get("number"),
                    order.get("status"),
                    order.get("menuItemId"),
                    menu_item.get("ru"),
                    menu_item.get("en"),
                    order.get("totalSum"),
                    order.get("terminal"),
                    order.get("kioskId"),
                    order.get("userId"),
                    order.get("franchiseeId"),
                    order.get("receivedAt"),
                    order.get("brewedAt"),
                    order.get("completedAt"),
                    json.dumps(order.get("productDump"), ensure_ascii=False),
                    json.dumps(order, ensure_ascii=False),
                    fetched_at,
                ),
            )

    def count_orders(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM fibbee_orders").fetchone()[0]

    # ---- тикеты ---------------------------------------------------------------

    def save_ticket(self, ticket: dict, fetched_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fibbee_tickets (
                    ticket_id, number, node, zone, status, state, category,
                    priority, source, description, sales_point_ids,
                    incident_time, incident_end_time, discord_link,
                    created_at, created_by, changed_by, updated_at,
                    raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.get("ticketId"),
                    ticket.get("number"),
                    ticket.get("node"),
                    ticket.get("zone"),
                    ticket.get("status"),
                    ticket.get("state"),
                    ticket.get("category"),
                    ticket.get("priority"),
                    ticket.get("source"),
                    ticket.get("description"),
                    json.dumps(ticket.get("salesPointIds"), ensure_ascii=False),
                    ticket.get("incidentTime"),
                    ticket.get("incidentEndTime"),
                    ticket.get("discordLink"),
                    ticket.get("createdAt"),
                    ticket.get("createdBy"),
                    ticket.get("changedBy"),
                    ticket.get("updatedAt"),
                    json.dumps(ticket, ensure_ascii=False),
                    fetched_at,
                ),
            )

    def count_tickets(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM fibbee_tickets").fetchone()[0]

    # ---- аудит-лог --------------------------------------------------------------

    def save_change(self, change: dict, fetched_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fibbee_changes_log (
                    change_id, type, object_id, changed_by, updated_at,
                    changes_json, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change.get("changeId"),
                    change.get("type"),
                    change.get("objectId"),
                    change.get("changedBy"),
                    change.get("updatedAt"),
                    json.dumps(change.get("changes"), ensure_ascii=False),
                    json.dumps(change, ensure_ascii=False),
                    fetched_at,
                ),
            )

    def count_changes(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM fibbee_changes_log").fetchone()[0]

    # ---- остатки по устройствам --------------------------------------------------

    def save_healthcheck(self, sales_point_id: str, healthcheck: dict, fetched_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fibbee_healthchecks (sales_point_id, raw_json, fetched_at) VALUES (?, ?, ?)",
                (sales_point_id, json.dumps(healthcheck, ensure_ascii=False), fetched_at),
            )
