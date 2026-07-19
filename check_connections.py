"""
Проверка живости всех коннекторов одним запуском.

Для каждого коннектора запускает его собственный `healthcheck.py` в его же
venv (лёгкий запрос, не полный sync — например "кто я" вместо скачивания
всех заявок), печатает прогресс-бар и итоговую таблицу OK / НЕ OK.

Все детали (stdout, stderr, traceback) всегда пишутся в logs/connections_check.log,
даже для успешных проверок — если что-то не OK, просто пришли мне путь к
этому файлу (или его содержимое), не нужно пересказывать своими словами.

Запуск:
    python check_connections.py
"""

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONNECTORS_DIR = ROOT / "connectors"
CORE_DIR = ROOT / "core"
LOG_PATH = ROOT / "logs" / "connections_check.log"

# (папка в connectors/, отображаемое имя, таймаут в секундах)
# Таймауты трекера/Service Desk — с запасом сверх таймаута ОДНОГО запроса на
# стороне клиента (TIMEOUT_SECONDS в их client.py: 30с и 60с соответственно;
# их healthcheck.py дёргает клиент с retries=1, так что тут не нужно
# умножать на 3 попытки, но небольшой запас на накладные расходы нужен).
CONNECTORS = [
    ("discord", "Discord", 20),
    ("gmail", "Gmail", 30),
    ("drive", "Google Drive", 30),
    ("tracker", "Tracker", 45),
    ("servicedesk", "Service Desk", 80),
    ("fibbee", "Fibbee ERP", 45),
    ("sheets_export", "Google Sheets (общая таблица)", 30),
]

# Не коннекторы источников данных, а сервисные модули в core/ — тот же
# healthcheck.py-контракт (OK/FAIL), просто из другой директории.
CORE_SERVICES = [
    ("router_ai", "Router AI (OpenRouter + Gemini)", 30),
]

BAR_WIDTH = 30


def _python_for(connector_dir: Path) -> str:
    """Свой venv коннектора, если есть — иначе текущий интерпретатор."""
    for candidate in (
        connector_dir / "venv" / "Scripts" / "python.exe",  # Windows
        connector_dir / "venv" / "bin" / "python",  # macOS/Linux
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _draw_bar(done: int, total: int, label: str):
    filled = int(BAR_WIDTH * done / total) if total else BAR_WIDTH
    bar = "#" * filled + "-" * (BAR_WIDTH - filled)
    # Хвостовые пробелы затирают предыдущую, более длинную строку в той же позиции.
    print(f"\r[{bar}] {done}/{total} {label}" + " " * 25, end="", flush=True)


def run_check(connector_dir: Path, timeout: int) -> dict:
    healthcheck = connector_dir / "healthcheck.py"
    if not healthcheck.exists():
        return {
            "ok": False,
            "detail": "healthcheck.py не найден",
            "elapsed": 0.0,
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }

    python = _python_for(connector_dir)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [python, str(healthcheck)],
            cwd=str(connector_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        ok = proc.returncode == 0 and stdout.startswith("OK")
        detail = stdout[3:].strip() if ok else (stdout or (stderr.splitlines()[-1] if stderr else "нет вывода, код выхода %s" % proc.returncode))
        return {
            "ok": ok,
            "detail": detail,
            "elapsed": elapsed,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        return {
            "ok": False,
            "detail": f"не ответил за {timeout}с (похоже на TLS-фильтрацию/сеть — см. лог)",
            "elapsed": elapsed,
            "stdout": (e.stdout or ""),
            "stderr": (e.stderr or ""),
            "returncode": None,
        }
    except OSError as e:
        return {
            "ok": False,
            "detail": f"не смог запустить: {e}",
            "elapsed": time.monotonic() - started,
            "stdout": "",
            "stderr": str(e),
            "returncode": None,
        }


def main():
    all_checks = [(CONNECTORS_DIR, slug, label, timeout) for slug, label, timeout in CONNECTORS]
    all_checks += [(CORE_DIR, slug, label, timeout) for slug, label, timeout in CORE_SERVICES]
    total = len(all_checks)
    results = []

    _draw_bar(0, total, "старт...")
    for i, (base_dir, slug, label, timeout) in enumerate(all_checks, start=1):
        _draw_bar(i - 1, total, f"проверяю {label}...")
        result = run_check(base_dir / slug, timeout)
        result["slug"] = slug
        result["label"] = label
        results.append(result)
        _draw_bar(i, total, f"{label}: {'OK' if result['ok'] else 'НЕ OK'}")

    print("\n")
    name_width = max(len(r["label"]) for r in results) + 2
    print(f"{'Коннектор':<{name_width}}{'Статус':<8}{'Время':<8}Детали")
    print("-" * 70)
    any_fail = False
    for r in results:
        status = "OK" if r["ok"] else "НЕ OK"
        if not r["ok"]:
            any_fail = True
        print(f"{r['label']:<{name_width}}{status:<8}{r['elapsed']:.1f}с   {r['detail'][:60]}")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(f"Проверка подключений — {datetime.now(timezone.utc).isoformat()}\n")
        f.write("=" * 70 + "\n\n")
        for r in results:
            status = "OK" if r["ok"] else "НЕ OK"
            f.write(f"## {r['label']} ({r['slug']}) — {status}, {r['elapsed']:.1f}с\n")
            f.write(f"exit code: {r['returncode']}\n")
            f.write(f"stdout:\n{r['stdout']}\n")
            if r["stderr"]:
                f.write(f"stderr:\n{r['stderr']}\n")
            f.write("\n")

    print()
    if any_fail:
        print(f"Есть проблемы — подробности в {LOG_PATH}")
        sys.exit(1)
    else:
        print("Все коннекторы в порядке.")
        sys.exit(0)


if __name__ == "__main__":
    main()
