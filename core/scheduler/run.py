"""
Планировщик синхронизаций коннекторов — замена Windows Task Scheduler.

На машине пользователя `schtasks /create` отдаёт "Отказано в доступе" (нет
прав администратора/групповая политика) — этот скрипт делает то же самое
изнутри обычного пользовательского процесса, без повышенных прав. Работает
как persistent-процесс, запускается через папку автозагрузки Windows (см.
`SETUP_NEW_USER.md`), а не через Планировщик заданий.

Правила (по просьбе пользователя, "работа с гугл драйвом должна выполняться
автоматически либо во время простоя, либо ночью"):
- Drive и Gmail — раз в сутки, либо в NIGHT_HOUR ночи, либо раньше — если
  компьютер простаивает (нет ввода с клавиатуры/мыши) дольше IDLE_MINUTES;
  какое условие наступит раньше в течение суток.
- Tracker/Service Desk/Fibbee/sheets_export — каждые SYNC_INTERVAL_HOURS
  часов: их данные показываются на экране "Задачи", в отчётах и в общей
  Google-таблице открытых тикетов, раз в сутки было бы слишком редко.
  sheets_export должен идти ПОСЛЕ servicedesk/fibbee в списке — он читает их
  локальные SQLite напрямую, порядок в FREQUENT_JOBS ниже это отражает, но
  раз джобы асинхронны (см. _run_sync_async), гарантии порядка нет: в худшем
  случае одна синхронизация отстанет на один цикл (SYNC_INTERVAL_HOURS),
  не критично для статусной таблицы.

Простой определяется через WinAPI GetLastInputInfo — штатный способ, прав
администратора не требует.

Запуск: обычный `python run.py` (только стандартная библиотека, отдельный
venv не нужен).
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "scheduler.log"
STATE_PATH = ROOT / "logs" / "scheduler_state.json"

NIGHT_HOUR = 3           # ночная синхронизация Drive/Gmail — 03:xx
IDLE_MINUTES = 15        # или раньше, если простаивает дольше этого
CHECK_INTERVAL_SECONDS = 5 * 60
SYNC_INTERVAL_HOURS = 2  # Tracker/Service Desk/Fibbee

NIGHTLY_JOBS = ["drive", "gmail"]
FREQUENT_JOBS = ["tracker", "servicedesk", "fibbee", "sheets_export"]


def _log(msg: str):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _idle_minutes() -> float:
    """WinAPI GetLastInputInfo — сколько минут прошло с последнего ввода с клавиатуры/мыши."""

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    millis_idle = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    return millis_idle / 1000 / 60


_state_lock = threading.Lock()
_running_jobs: set[str] = set()


def _run_sync_async(job: str, key: str, state: dict, state_value: str):
    """
    Запускает синхронизацию job в отдельном потоке и по завершении обновляет
    state под локом.

    Раньше _run_sync() вызывался напрямую в главном цикле — это значило, что
    он блокировал главный цикл до конца (subprocess.run с timeout=3600), и
    задания шли строго одно за другим: медленная синхронизация Drive
    откладывала Tracker/SD/Fibbee на час. Теперь каждое задание — отдельный
    поток, синхронизации идут параллельно, главный цикл не блокируется.
    _running_jobs не даёт двум потокам одновременно синхронизировать один и
    тот же job (например, если предыдущий запуск ещё не закончился к моменту
    следующей проверки).
    """
    with _state_lock:
        if job in _running_jobs:
            _log(f"{job}: предыдущий запуск ещё выполняется, пропуск")
            return
        _running_jobs.add(job)
    try:
        _run_sync(job)
    finally:
        with _state_lock:
            state[key] = state_value
            _save_state(state)
            _running_jobs.discard(job)


def _run_sync(job: str):
    connector_dir = ROOT / "connectors" / job
    python = connector_dir / "venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = connector_dir / "venv" / "bin" / "python"
    log_file = ROOT / "logs" / f"{job}_sync.log"
    _log(f"старт синхронизации: {job}")
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"\n=== scheduler run {datetime.now().isoformat()} ===\n")
            proc = subprocess.run(
                [str(python), "sync.py"], cwd=str(connector_dir), stdout=lf, stderr=subprocess.STDOUT, timeout=3600
            )
        _log(f"{job}: завершено, код выхода {proc.returncode} (подробности — {log_file})")
    except subprocess.TimeoutExpired:
        _log(f"{job}: не уложился в час, прерван")
    except Exception as e:
        _log(f"{job}: ошибка запуска — {e}")


def main():
    _log("планировщик запущен")
    state = _load_state()

    while True:
        now = datetime.now()
        today = now.date().isoformat()
        idle = _idle_minutes()

        for job in NIGHTLY_JOBS:
            key = f"{job}_last_run_date"
            if state.get(key) != today and (now.hour == NIGHT_HOUR or idle >= IDLE_MINUTES):
                reason = "ночь" if now.hour == NIGHT_HOUR else f"простой {idle:.0f} мин"
                _log(f"{job}: условие выполнено ({reason})")
                threading.Thread(
                    target=_run_sync_async, args=(job, key, state, today), daemon=True
                ).start()

        for job in FREQUENT_JOBS:
            key = f"{job}_last_run_at"
            last = state.get(key)
            due = True
            if last:
                try:
                    due = now - datetime.fromisoformat(last) >= timedelta(hours=SYNC_INTERVAL_HOURS)
                except ValueError:
                    due = True
            if due:
                threading.Thread(
                    target=_run_sync_async, args=(job, key, state, now.isoformat()), daemon=True
                ).start()

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Этот планировщик рассчитан на Windows (WinAPI для определения простоя).")
        sys.exit(1)
    main()
