"""
Fibbee ERP sync — через внутренний JSON API erp.fibbee.com (email+password
логин, JWT в заголовке x-auth-token, см. client.py).

Тянет:
- полный список комплексов (sales-points/list) и остатки по устройствам
  (sales-points/healthchecks) — полный снимок на каждый запуск, данных мало
  (десятки-сотни точек);
- заказы (orders/list) за последние FIBBEE_SYNC_DAYS дней по ВСЕМ точкам
  сразу — startDate/endDate реально фильтрует на сервере (проверено), так
  что просто одна пагинация offset+limit по всему окну;
- тикеты (tickets/list) и аудит-лог (changes-log/list) за то же окно —
  **эти два эндпоинта дату НЕ фильтруют** (проверено контрольным
  сравнением: узкий/широкий/отсутствующий диапазон дают одинаковый
  результат), но список отсортирован по убыванию даты, поэтому листаем
  offset+limit и останавливаемся сами, как только видим запись старше
  окна — плюс жёсткий потолок страниц на случай, если сортировка вдруг
  не так строга, как кажется.

incidents/list не синхронизируется здесь: эндпоинт требует salesPointId
(без него зависает на сервере), гонять его по всем точкам на каждый sync —
слишком тяжело. Метод client.get_incidents(sales_point_id) доступен для
точечных запросов агентом по конкретному комплексу.

Запись (изменение тикетов, управление оборудованием и т.п.) не входит в
sync — см. client.py и README.md за write-методами и предупреждениями.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from client import FibbeeAuthError, FibbeeClient
from storage import FibbeeStorage

load_dotenv()

BASE_URL = os.environ.get("FIBBEE_BASE_URL", "https://erp.fibbee.com")
EMAIL = os.environ.get("FIBBEE_EMAIL", "")
PASSWORD = os.environ.get("FIBBEE_PASSWORD", "")
TOKEN_PATH = os.environ.get("FIBBEE_TOKEN_PATH", "./fibbee_token.json")
DB_PATH = os.environ.get("DB_PATH", "./data/fibbee.db")
SYNC_DAYS = int(os.environ.get("FIBBEE_SYNC_DAYS", "2"))
PAGE_LIMIT = 1000
MAX_PAGES_UNBOUNDED = 200  # защита от рантвея для tickets/changes-log (без серверного total)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fibbee_connector")


def _sync_orders_window(client: FibbeeClient, storage: FibbeeStorage, start_str: str, end_str: str, fetched_at: str) -> int:
    """orders/list честно фильтрует по startDate/endDate на сервере — просто листаем офсетом."""
    offset = 0
    saved = 0
    while True:
        page = client.get_orders(start_date=start_str, end_date=end_str, limit=PAGE_LIMIT, offset=offset)
        orders = page.get("orders", [])
        total = page.get("total")
        for order in orders:
            storage.save_order(order, fetched_at)
            saved += 1
        if len(orders) < PAGE_LIMIT or (total is not None and offset + len(orders) >= total):
            break
        offset += PAGE_LIMIT
    return saved


def _sync_tickets_window(client: FibbeeClient, storage: FibbeeStorage, start_dt: datetime, fetched_at: str) -> int:
    """tickets/list НЕ фильтрует по дате на сервере (проверено) — отсортирован по убыванию
    createdAt, поэтому листаем и сами останавливаемся, когда ушли раньше start_dt."""
    offset = 0
    saved = 0
    start_ms = start_dt.timestamp() * 1000
    for _ in range(MAX_PAGES_UNBOUNDED):
        tickets = client.get_tickets(limit=PAGE_LIMIT, offset=offset)
        if not tickets:
            break
        stop = False
        for ticket in tickets:
            created_at = ticket.get("createdAt") or 0
            if created_at < start_ms:
                stop = True
                break
            storage.save_ticket(ticket, fetched_at)
            saved += 1
        if stop or len(tickets) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return saved


def _sync_changes_window(client: FibbeeClient, storage: FibbeeStorage, start_dt: datetime, fetched_at: str) -> int:
    """changes-log/list — тот же случай, что и tickets: дата не фильтруется сервером."""
    offset = 0
    saved = 0
    start_ms = start_dt.timestamp() * 1000
    for _ in range(MAX_PAGES_UNBOUNDED):
        changes = client.get_changes_log(limit=PAGE_LIMIT, offset=offset)
        if not changes:
            break
        stop = False
        for change in changes:
            updated_at = change.get("updatedAt") or 0
            if updated_at < start_ms:
                stop = True
                break
            storage.save_change(change, fetched_at)
            saved += 1
        if stop or len(changes) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return saved


def sync():
    if not EMAIL or not PASSWORD:
        log.error("FIBBEE_EMAIL/FIBBEE_PASSWORD не заданы в .env")
        return

    client = FibbeeClient(BASE_URL, EMAIL, PASSWORD, TOKEN_PATH)
    storage = FibbeeStorage(DB_PATH)
    fetched_at = datetime.now(timezone.utc).isoformat()

    try:
        sales_points = client.get_sales_points()
    except FibbeeAuthError as e:
        log.error(str(e))
        return

    log.info("Получено комплексов: %d", len(sales_points))
    for sp in sales_points:
        storage.save_sales_point(sp, fetched_at)

    healthchecks = client.get_sales_point_healthchecks()
    log.info("Получено healthchecks: %d точек", len(healthchecks))
    for sp_id, hc in healthchecks.items():
        storage.save_healthcheck(sp_id, hc, fetched_at)

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=SYNC_DAYS)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    start_str, end_str = start_date.isoformat(), end_date.isoformat()
    log.info("Окно синхронизации: %s .. %s (%d дн.)", start_str, end_str, SYNC_DAYS)

    orders_saved = _sync_orders_window(client, storage, start_str, end_str, fetched_at)
    log.info("Заказов сохранено: %d", orders_saved)

    tickets_saved = _sync_tickets_window(client, storage, start_dt, fetched_at)
    log.info("Тикетов сохранено: %d", tickets_saved)

    changes_saved = _sync_changes_window(client, storage, start_dt, fetched_at)
    log.info("Записей аудит-лога сохранено: %d", changes_saved)

    log.info(
        "Готово. В базе: %d комплексов, %d заказов, %d тикетов, %d записей лога изменений.",
        storage.count_sales_points(), storage.count_orders(), storage.count_tickets(), storage.count_changes(),
    )


if __name__ == "__main__":
    sync()
