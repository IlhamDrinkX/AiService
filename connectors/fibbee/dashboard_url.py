"""
Печатает рабочую ссылку на нативный дашборд комплекса Fibbee
(`/dashboard/view/{id}/dashboard.html?token=<JWT>`, см. README про находку
с WebSocket — сам дашборд SPA, но ссылка с валидным токеном открывается
нормально в браузере пользователя).

Используется веб-мордой (core/webapp) через subprocess в venv этого
коннектора — не тянем curl_cffi в зависимости веб-морды ради одной ссылки.

Запуск:
    python dashboard_url.py <sales_point_id>
Печатает URL в stdout (exit 0) или "FAIL <причина>" (exit 1).
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
    if len(sys.argv) != 2:
        print("FAIL использование: python dashboard_url.py <sales_point_id>")
        sys.exit(1)
    sales_point_id = sys.argv[1]

    if not EMAIL or not PASSWORD:
        print("FAIL FIBBEE_EMAIL/FIBBEE_PASSWORD не заданы в .env")
        sys.exit(1)

    try:
        client = FibbeeClient(BASE_URL, EMAIL, PASSWORD, TOKEN_PATH)
        # Токен уже есть после логина/загрузки из TOKEN_PATH — сама
        # get_dashboard_html() дёргает сеть, нам сеть не нужна, только токен,
        # но лёгкого публичного метода для этого в client.py нет, поэтому
        # один раз бьём тем же путём, что healthcheck (дешёвый list-запрос),
        # чтобы гарантированно освежить токен, если он истёк.
        client.get_sales_points(retries=1)
    except FibbeeAuthError as e:
        print(f"FAIL {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAIL {e}")
        sys.exit(1)

    print(f"{BASE_URL}/dashboard/view/{sales_point_id}/dashboard.html?token={client.token}")
    sys.exit(0)


if __name__ == "__main__":
    main()
