"""
Лёгкая проверка коннектора трекера: один минимальный запрос (/api/me)
через уже существующий TrackerClient — та же curl_cffi cookie-сессия,
что и sync.py.

retries=1 (не стандартные 3 из sync.py) — это именно health check: если
соединение реально режется на уровне TLS/WAF, повтор через 3/6/9 секунд
ничего не изменит, а вот таймаут проверки раздувается втрое. sync.py при
настоящей синхронизации всё ещё делает полные 3 попытки.

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1).
"""

import os
import sys

from dotenv import load_dotenv

from client import TrackerAuthError, TrackerClient

load_dotenv()

BASE_URL = os.environ.get("TRACKER_BASE_URL", "https://tracker.drinkx.tech")
STATE_PATH = os.environ.get("TRACKER_STATE_PATH", "./tracker_state.json")


def main():
    try:
        client = TrackerClient(BASE_URL, state_path=STATE_PATH)
        me = client.get_me(retries=1)
    except TrackerAuthError as e:
        print(f"FAIL {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAIL {e} (один быстрый запрос без повторов — попробуй python sync.py для полной картины с retry)")
        sys.exit(1)

    print(f"OK залогинен как {me.get('name')} <{me.get('email')}>")
    sys.exit(0)


if __name__ == "__main__":
    main()
