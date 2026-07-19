"""
Фоновая проверка подключений для баннера алертов в веб-морде (по просьбе
пользователя: "если к чему то нет подключения должен появиться алерт и
сообщение что не так").

Переиспользует `check_connections.py` из корня проекта (тот же список
коннекторов, тот же способ запуска healthcheck.py в venv каждого коннектора)
вместо дублирования — единственное отличие: тут результат складывается в
JSON-файл, а не печатается в консоль, и это происходит периодически в фоновом
потоке, а не по разовому запуску вручную.

Проверка не гоняется на каждый HTTP-запрos (некоторые healthcheck, например
Service Desk, тайм-аут до 80с — 6-7 проверок подряд на каждый заход на любую
страницу были бы неприемлемо медленными) — раз в INTERVAL_SECONDS в фоновом
потоке, веб-морда только читает последний закэшированный результат.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import check_connections as cc  # noqa: E402

STATUS_PATH = Path(__file__).resolve().parent / "data" / "health_status.json"
INTERVAL_SECONDS = 20 * 60  # раз в 20 минут — компромисс между свежестью и накладными расходами


def run_all_checks() -> dict:
    results = []
    for base_dir, slug, label, timeout in (
        [(cc.CONNECTORS_DIR, s, l, t) for s, l, t in cc.CONNECTORS]
        + [(cc.CORE_DIR, s, l, t) for s, l, t in cc.CORE_SERVICES]
    ):
        r = cc.run_check(base_dir / slug, timeout)
        results.append({"slug": slug, "label": label, "ok": r["ok"], "detail": r["detail"]})
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "results": results}


def _write_status(status: dict):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def read_status() -> dict | None:
    if not STATUS_PATH.exists():
        return None
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _loop():
    while True:
        try:
            status = run_all_checks()
            _write_status(status)
        except Exception as e:  # фоновый поток не должен ронять сервер
            _write_status(
                {
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "results": [],
                    "error": f"health_monitor упал: {e}",
                }
            )
        time.sleep(INTERVAL_SECONDS)


def start_background_monitor():
    thread = threading.Thread(target=_loop, daemon=True, name="health_monitor")
    thread.start()
