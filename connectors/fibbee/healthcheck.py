"""
Лёгкая проверка Fibbee-коннектора: дёргает /v1/sales-points/list через
существующий FibbeeClient (та же сессия/логин, что и sync.py).

retries=1 — при настоящем сетевом/WAF-блоке повтор всё равно не поможет, а
таймаут проверки утроится. sync.py при реальной синхронизации делает
полные 3 попытки.

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1).
"""

import os
import sys

from dotenv import load_dotenv

from client import FibbeeAuthError, FibbeeClient

load_dotenv()

BASE_URL = os.environ.get("FIBBEE_BASE_URL", "https://erp.fibbee.com")
EMAIL = os.environ.get("FIBBEE_EMAIL", "")
PASSWORD = os.environ.get("FIBBEE_PASSWORD", "")
TOKEN_PATH = os.environ.get("FIBBEE_TOKEN_PATH", "./fibbee_token.json")


def main():
    if not EMAIL or not PASSWORD:
        print("FAIL FIBBEE_EMAIL/FIBBEE_PASSWORD не заданы в .env")
        sys.exit(1)

    try:
        client = FibbeeClient(BASE_URL, EMAIL, PASSWORD, TOKEN_PATH)
        sales_points = client.get_sales_points(retries=1)
    except FibbeeAuthError as e:
        print(f"FAIL {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAIL {e} (один быстрый запрос без повторов — попробуй python sync.py для полной картины с retry)")
        sys.exit(1)

    print(f"OK точек: {len(sales_points)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
