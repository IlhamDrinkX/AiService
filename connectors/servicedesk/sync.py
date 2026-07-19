"""
Service Desk sync — read-only, через внутренний JSON API sd.drinkx.tech
(cookie-сессия после Google SSO, см. login.py и client.py). Запись
(создание/обновление заявок) не реализована и не планируется без отдельного
запроса — см. README.md.
"""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from client import ServiceDeskAuthError, ServiceDeskClient
from storage import ServiceDeskStorage

load_dotenv()

BASE_URL = os.environ.get("SERVICEDESK_BASE_URL", "https://sd.drinkx.tech")
STATE_PATH = os.environ.get("SERVICEDESK_STATE_PATH", "./servicedesk_state.json")
DB_PATH = os.environ.get("DB_PATH", "./data/servicedesk.db")
FILES_DIR = os.environ.get("FILES_DIR", "./data/files")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("servicedesk_connector")


def sync():
    client = ServiceDeskClient(BASE_URL, STATE_PATH)
    storage = ServiceDeskStorage(DB_PATH, FILES_DIR)
    fetched_at = datetime.now(timezone.utc).isoformat()

    try:
        tickets = client.get_tickets()
    except ServiceDeskAuthError as e:
        log.error(str(e))
        return

    log.info("Получено заявок: %d", len(tickets))
    for t in tickets:
        storage.save_ticket(t, fetched_at)

    log.info(
        "Готово. Заявок в базе: %d, файлов сохранено: %d",
        storage.count_tickets(),
        storage.count_files(),
    )

    # Клиенты (Day2Day, Вектор, ...) — пока не синкаются, путь эндпоинта не
    # подтверждён (см. client.get_clients()). Раскомментировать после уточнения:
    # try:
    #     for c in client.get_clients():
    #         storage.save_client(c, fetched_at)
    # except ServiceDeskAuthError as e:
    #     log.error(str(e))


if __name__ == "__main__":
    sync()
