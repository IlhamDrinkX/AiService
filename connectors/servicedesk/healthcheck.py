"""
Лёгкая проверка Service Desk-коннектора: единственный подтверждённый
эндпоинт — /api/prototype/tickets, поэтому проверка дёргает именно его через
существующий ServiceDeskClient (та же curl_cffi-сессия и cookie, что и
sync.py). Тяжелее, чем хотелось бы (полный список заявок), но других
эндпоинтов пока нет — см. README про auth-check/clients.

retries=1 (не стандартные 3 из sync.py) — при настоящем TLS/WAF-блоке повтор
через 3/6/9 секунд ничего не изменит, а вот таймаут проверки утроится.
sync.py при настоящей синхронизации всё ещё делает полные 3 попытки.

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1).
"""

import os
import sys

from dotenv import load_dotenv

from client import ServiceDeskAuthError, ServiceDeskClient

load_dotenv()

BASE_URL = os.environ.get("SERVICEDESK_BASE_URL", "https://sd.drinkx.tech")
STATE_PATH = os.environ.get("SERVICEDESK_STATE_PATH", "./servicedesk_state.json")


def main():
    try:
        client = ServiceDeskClient(BASE_URL, STATE_PATH)
        tickets = client.get_tickets(retries=1)
    except ServiceDeskAuthError as e:
        print(f"FAIL {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAIL {e} (один быстрый запрос без повторов — попробуй python sync.py для полной картины с retry)")
        sys.exit(1)

    print(f"OK заявок: {len(tickets)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
