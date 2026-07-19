"""
Фоновые задачи для долгих LLM-запросов (отчёты/база знаний/Discord-сводки).

Зачем: раньше POST-запрос (например /reports/generate) держал HTTP-соединение
открытым, пока модель не ответит (иногда 20-40 секунд) — если пользователь
переключался на другую вкладку ПРИЛОЖЕНИЯ (не браузера — обычная навигация
htmx/новая страница), запрос обрывался вместе со старой страницей, и результат
терялся безвозвратно. По просьбе пользователя (2026-07-19): нужен явный
прогресс-индикатор, и результат не должен теряться при уходе со страницы —
по возвращении должен показаться готовый ответ, если запрос уже был сделан.

Решение: сама генерация уходит в фоновый поток сразу же (create_job), запрос
к серверу возвращается мгновенно с job_id и вёрсткой, которая опрашивает
статус (`hx-trigger="load, every 1s"`); job_id также кладётся в localStorage
на стороне браузера (см. `<script>` в *_result.html/*_summary.html) — при
повторном заходе на страницу JS сам поднимает последний job_id и показывает
его текущее состояние (ещё считает или уже готово), не только для активной
вкладки браузера в момент ответа.

Хранилище — простой dict в памяти процесса: это однопользовательское
локальное приложение на одном ноутбуке, не сервис на много пользователей —
переживать перезапуск веб-морды не обязано (как и остальное состояние
процесса, например health_monitor).
"""

from __future__ import annotations

import threading
import time
import uuid

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

# Старые задачи чистим, чтобы _JOBS не рос бесконечно на долго работающем
# процессе — результат нужен только чтобы пережить одну сессию просмотра,
# не архив на все времена.
_MAX_AGE_SECONDS = 2 * 60 * 60


def _cleanup_locked():
    cutoff = time.time() - _MAX_AGE_SECONDS
    stale = [jid for jid, j in _JOBS.items() if j["started_at"] < cutoff]
    for jid in stale:
        del _JOBS[jid]


def create_job(fn, *args, **kwargs) -> str:
    """Запускает fn(*args, **kwargs) в фоновом потоке, возвращает job_id
    сразу же, не дожидаясь завершения. fn должна вернуть dict — он же
    попадёт в job["result"] по завершении."""
    job_id = uuid.uuid4().hex
    with _LOCK:
        _cleanup_locked()
        _JOBS[job_id] = {"status": "running", "result": None, "error": None, "started_at": time.time()}

    def _run():
        try:
            result = fn(*args, **kwargs)
            with _LOCK:
                _JOBS[job_id]["status"] = "done"
                _JOBS[job_id]["result"] = result
        except Exception as e:  # фоновый поток не должен ронять сервер
            with _LOCK:
                _JOBS[job_id]["status"] = "error"
                _JOBS[job_id]["error"] = str(e)

    threading.Thread(target=_run, daemon=True, name=f"job-{job_id[:8]}").start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        return _JOBS.get(job_id)
