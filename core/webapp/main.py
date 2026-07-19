"""
Веб-морда (модуль UI из functional_plan_ui.md): FastAPI + HTMX + Jinja2,
тёмная тема, рендер на сервере — без Node-сборки. Работает локально на
ноутбуке пользователя.

Запуск (Windows, venv):
    cd core\\webapp
    python -m venv venv
    venv\\Scripts\\pip install -r requirements.txt -r ..\\router_ai\\requirements.txt
    venv\\Scripts\\python -m uvicorn main:app --reload --port 9000

Или просто запустить core\\webapp\\start.bat (ставит зависимости при первом
запуске и открывает браузер сам).

Открыть: http://127.0.0.1:9000
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router_ai"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "reporting"))
from client import RouterAIClient, RouterAIError  # noqa: E402
import generate as reporting  # noqa: E402
import data_sources as ds  # noqa: E402
import highlight  # noqa: E402
import kb_index  # noqa: E402
import usage_stats  # noqa: E402
import health_monitor  # noqa: E402
import jobs  # noqa: E402
import complex_analysis  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FIBBEE_DIR = ROOT / "connectors" / "fibbee"

app = FastAPI(title="AI Service System")
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


@app.on_event("startup")
def _start_health_monitor():
    health_monitor.start_background_monitor()

_client: RouterAIClient | None = None


def get_client() -> RouterAIClient:
    global _client
    if _client is None:
        _client = RouterAIClient()
    return _client


@app.get("/", response_class=RedirectResponse)
def root():
    return RedirectResponse("/tasks")


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", {"active": "help"})


@app.get("/health")
def health_json():
    return health_monitor.read_status() or {"checked_at": None, "results": [], "note": "проверка ещё не выполнялась"}


@app.get("/health/banner", response_class=HTMLResponse)
def health_banner(request: Request):
    status = health_monitor.read_status()
    broken = [r for r in (status or {}).get("results", []) if not r["ok"]] if status else []
    return templates.TemplateResponse(
        request, "health_banner.html", {"status": status, "broken": broken}
    )


def _run_health_check_job() -> dict:
    """Полный прогон run_all_checks() занимает до пары минут (несколько
    healthcheck.py с таймаутами до 80с, по очереди) — синхронно на кнопку
    было бы ровно то, от чего просили избавиться (замороженная страница без
    обратной связи), поэтому тоже через jobs.py."""
    status = health_monitor.run_all_checks()
    health_monitor._write_status(status)
    return {"health": status}


@app.post("/health/check-now", response_class=HTMLResponse)
def health_check_now(request: Request):
    job_id = jobs.create_job(_run_health_check_job)
    return templates.TemplateResponse(
        request, "_job_poll.html",
        {
            "poll_id": "healthCheckJob", "poll_url": f"/health/check-now/status/{job_id}",
            "storage_key": "health_check_last_job", "job_id": job_id, "label": "Проверяю источники",
        },
    )


@app.get("/health/check-now/status/{job_id}", response_class=HTMLResponse)
def health_check_now_status(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_health_panel.html", {"health": health_monitor.read_status()})
    if job["status"] == "running":
        return templates.TemplateResponse(
            request, "_job_poll.html",
            {
                "poll_id": "healthCheckJob", "poll_url": f"/health/check-now/status/{job_id}",
                "storage_key": "health_check_last_job", "job_id": job_id, "label": "Проверяю источники",
            },
        )
    if job["status"] == "error":
        return templates.TemplateResponse(request, "_health_panel.html", {"health": health_monitor.read_status()})
    return templates.TemplateResponse(request, "_health_panel.html", {"health": job["result"]["health"]})


# ------------------------------------------------------------------ #
# Управление процессом — рестарт/стоп/принудительная синхронизация
# (по просьбе пользователя, 2026-07-19: "нужна кнопка... сделает рестарт,
# выключит, прогрузит новые данные из гугл диска вне графика")
# ------------------------------------------------------------------ #

STOP_FLAG_PATH = Path(__file__).resolve().parent / "STOP_FLAG"


def _stale_job_response(
    storage_key: str,
    note: str = "Незавершённый запрос не сохранился (веб-морда перезапускалась) — просто повтори действие.",
) -> HTMLResponse:
    """"Job не найден" почти всегда значит одно: веб-морда перезапускалась
    (in-memory job store в jobs.py не переживает рестарт, см. комментарий
    там), а localStorage браузера остался со старым job_id. Без явной
    очистки при каждом следующем заходе на страницу JS опять пытался его
    поднять и опять получал "не найден" — выглядело как повторяющаяся
    ошибка (см. скриншот пользователя, 2026-07-20), хотя это ожидаемо и
    безобидно. Чистим ключ сразу и показываем нейтральную, а не красную
    "ошибка"-заметку — это не сбой, а обычное следствие рестарта."""
    return HTMLResponse(f'<p class="muted">{note}</p><script>localStorage.removeItem("{storage_key}");</script>')


@app.post("/admin/restart", response_class=HTMLResponse)
def admin_restart(request: Request):
    # Просто завершаем процесс — supervisor.bat (restart-loop, см.
    # SETUP_NEW_USER.md) поднимет новый через несколько секунд сам.
    # Небольшая задержка — чтобы HTTP-ответ успел уйти в браузер до выхода.
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return HTMLResponse(
        '<div class="panel">Перезапускаюсь... страница станет доступна через 5-10 секунд, обнови её вручную.</div>'
    )


@app.post("/admin/stop", response_class=HTMLResponse)
def admin_stop(request: Request):
    # В отличие от restart — supervisor.bat не должен поднять процесс снова.
    # STOP_FLAG проверяется в начале каждой итерации цикла supervisor.bat
    # (см. файл) — если он есть, цикл завершается вместо перезапуска питона.
    STOP_FLAG_PATH.write_text("stop", encoding="utf-8")
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return HTMLResponse(
        '<div class="panel">Останавливаюсь. Запустить заново — через ярлык автозагрузки '
        "(перезайти в систему) или вручную через supervisor.bat.</div>"
    )


@app.post("/admin/stop_all", response_class=HTMLResponse)
def admin_stop_all(request: Request):
    """Останавливает ВСЕ три процесса системы, не только веб-морду (см.
    /admin/stop выше). По просьбе пользователя (2026-07-19): "нужна кнопка
    остановить весь процесс". Discord-бот и планировщик — отдельные python-
    процессы каждый со своим supervisor.bat (connectors/discord/,
    core/scheduler/), у веб-морды нет прямого способа их выключить, как
    саму себя (os._exit убивает только собственный процесс). Поэтому:
    1) пишем STOP_FLAG в их директории — тот же приём, что уже был у
       веб-морды, оба supervisor.bat теперь тоже проверяют флаг (см. правки
       там же) и не перезапустят процесс снова;
    2) убиваем сами python.exe-процессы bot.py/run.py через PowerShell/WMI
       по командной строке — программно отправить им Ctrl+C на Windows
       нельзя, а STOP_FLAG сам по себе только не даст supervisor'у
       перезапустить их ПОСЛЕ следующего завершения, а не завершает их
       прямо сейчас;
    3) как обычно, самоостанов веб-морды (свой STOP_FLAG + os._exit).

    Кнопка НЕ привязана к закрытию вкладки браузера (window.beforeunload) —
    сознательный выбор: тот же обработчик срабатывает и на F5, и на обычную
    навигацию по приложению в части браузеров, так что автостоп по закрытию
    вкладки иногда убивал бы всегда-работающие Discord-бота и планировщик,
    когда пользователь просто обновил страницу. Ручная кнопка безопаснее."""
    discord_dir = ROOT / "connectors" / "discord"
    scheduler_dir = ROOT / "core" / "scheduler"
    for d in (discord_dir, scheduler_dir):
        try:
            (d / "STOP_FLAG").write_text("stop", encoding="utf-8")
        except OSError:
            pass

    ps_kill = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'bot\\.py' -or $_.CommandLine -match 'run\\.py' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_kill], timeout=15, capture_output=True)
    except Exception:
        pass  # веб-морда всё равно остановится ниже — не блокируем на этом шаге

    STOP_FLAG_PATH.write_text("stop", encoding="utf-8")
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return HTMLResponse(
        '<div class="panel">Останавливаю всё: веб-морду, Discord-бота и планировщик. '
        "Запустить заново — перезайти в систему (автозагрузка) или вручную через supervisor.bat "
        "в каждой из трёх папок (core/webapp, connectors/discord, core/scheduler).</div>"
    )


def _run_drive_sync_job() -> dict:
    connector_dir = ROOT / "connectors" / "drive"
    python = connector_dir / "venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = connector_dir / "venv" / "bin" / "python"
    if not python.exists():
        return {"ok": False, "output": f"venv не найден: {python}"}
    try:
        proc = subprocess.run(
            [str(python), "sync.py"], cwd=str(connector_dir),
            capture_output=True, text=True, timeout=3600,
        )
        tail = (proc.stdout or "")[-2500:]
        if proc.returncode != 0:
            tail += "\n--- stderr ---\n" + (proc.stderr or "")[-1500:]
        return {"ok": proc.returncode == 0, "output": tail.strip() or "(пустой вывод)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Не уложился в час, синхронизация прервана."}
    except Exception as e:
        return {"ok": False, "output": str(e)}


@app.post("/admin/sync/drive", response_class=HTMLResponse)
def admin_sync_drive(request: Request):
    job_id = jobs.create_job(_run_drive_sync_job)
    return templates.TemplateResponse(
        request, "_job_poll.html",
        {
            "poll_id": "driveSyncJob", "poll_url": f"/admin/sync/drive/status/{job_id}",
            "storage_key": "drive_sync_last_job", "job_id": job_id, "label": "Синхронизирую Google Drive",
        },
    )


@app.get("/admin/sync/drive/status/{job_id}", response_class=HTMLResponse)
def admin_sync_drive_status(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return _stale_job_response("drive_sync_last_job")
    if job["status"] == "running":
        return templates.TemplateResponse(
            request, "_job_poll.html",
            {
                "poll_id": "driveSyncJob", "poll_url": f"/admin/sync/drive/status/{job_id}",
                "storage_key": "drive_sync_last_job", "job_id": job_id, "label": "Синхронизирую Google Drive",
            },
        )
    if job["status"] == "error":
        return HTMLResponse(f'<div class="panel" style="border-color:var(--danger)">Ошибка: {job["error"]}</div>')
    result = job["result"]
    return templates.TemplateResponse(request, "admin_job_result.html", {"result": result})


# ------------------------------------------------------------------ #
# Модуль 1 — задачи
# ------------------------------------------------------------------ #

@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    recent = ds.load_recently_updated(30)
    return templates.TemplateResponse(request, "tasks.html", {"active": "tasks", "recent": recent})


@app.get("/tasks/data")
def tasks_data():
    """
    Полный список задач как JSON — фильтрация/сортировка/поиск на экране
    "Задачи" сделаны на клиенте (объём данных вполне укладывается в память
    браузера), поэтому отдаём всё разом, без урезания до 50 записей.
    """
    data = ds.load_all_tasks(limit=5000)
    # без "text" (сырое описание) — на экране не показывается, только раздувает ответ
    return [{k: v for k, v in it.items() if k != "text"} for it in data["all"]]


# ------------------------------------------------------------------ #
# Модуль 1a — сводные отчёты
# ------------------------------------------------------------------ #

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    return templates.TemplateResponse(request, "reports.html", {"active": "reports"})


def _effective_provider(tier: str, provider: str) -> str | None:
    """pro — это Claude, бесплатного Gemini для него не бывает: что бы ни
    пришло из формы (в т.ч. если JS на странице не сработал и disabled-поле
    всё равно как-то попало в запрос), на pro всегда OpenRouter. None означает
    "не переопределять" — тогда чат берёт provider из tier в models.yaml."""
    if tier == "pro":
        return None
    return provider or None


def _build_report(source: str, days: float, question: str, tier: str, provider: str = ""):
    since = datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None
    sources = list(reporting.LOADERS.keys()) if source == "all" else [source]
    items = []
    for s in sources:
        items.extend(reporting.LOADERS[s](since))
    if not items:
        return None, [], "Нет данных за выбранный период — проверь, что sync.py запускался по нужным источникам."

    items.sort(key=lambda it: it.get("created_at") or "", reverse=True)
    truncated = ""
    if len(items) > reporting.MAX_ITEMS_DEFAULT:
        truncated = f"\n\n_(включены {reporting.MAX_ITEMS_DEFAULT} самых свежих из {len(items)})_"
        items = items[: reporting.MAX_ITEMS_DEFAULT]

    q = question.strip() or (
        f"Сделай сводку за последние {days} дн. по источникам {', '.join(sources)}: "
        f"сколько всего, разбивка по статусам, повторяющиеся проблемы, что требует внимания."
    )
    client = get_client()
    answer = client.report(
        question=q, items=items, tier=tier, task_label="report_web",
        provider=_effective_provider(tier, provider),
    ) + truncated
    return answer, items, None


def _run_report_job(source: str, days: float, question: str, tier: str, provider: str) -> dict:
    """Тело выполняется в фоновом потоке (jobs.create_job) — POST-роут не
    ждёт модель, сразу отдаёт вёрстку опроса статуса (см. jobs.py, docstring
    — по просьбе пользователя, чтобы уход со страницы не терял результат)."""
    try:
        answer, items, error = _build_report(source, days, question, tier, provider)
    except RouterAIError as e:
        return {"answer": None, "items": [], "error": f"Ошибка вызова модели: {e}"}
    return {"answer": answer, "items": items, "error": error}


def _render_report_result(request: Request, job_result: dict | None, fallback_error: str | None = None):
    if job_result is None:
        return templates.TemplateResponse(
            request, "reports_result.html",
            {"error": fallback_error, "items": [], "answer_html": None, "chart_json": None},
        )
    answer, items, error = job_result["answer"], job_result["items"], job_result["error"]
    chart_json = None
    html_answer = None
    if answer:
        clean_answer, chart = reporting.extract_chart(answer)
        html_answer = reporting._linkify(clean_answer, items, "html")
        if chart:
            import json

            chart_json = json.dumps(chart)
    return templates.TemplateResponse(
        request,
        "reports_result.html",
        {"answer_html": html_answer, "error": error, "items": items, "chart_json": chart_json},
    )


@app.post("/reports/generate", response_class=HTMLResponse)
def reports_generate(
    request: Request,
    source: str = Form("all"),
    days: float = Form(7),
    question: str = Form(""),
    tier: str = Form("flash"),
    provider: str = Form("gemini_direct"),
):
    job_id = jobs.create_job(_run_report_job, source, days, question, tier, provider)
    return templates.TemplateResponse(
        request, "_job_poll.html",
        {
            "poll_id": "reportJob", "poll_url": f"/reports/status/{job_id}",
            "storage_key": "reports_last_job", "job_id": job_id, "label": "Готовлю отчёт",
        },
    )


@app.get("/reports/status/{job_id}", response_class=HTMLResponse)
def reports_status(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return _stale_job_response("reports_last_job")
    if job["status"] == "running":
        return templates.TemplateResponse(
            request, "_job_poll.html",
            {
                "poll_id": "reportJob", "poll_url": f"/reports/status/{job_id}",
                "storage_key": "reports_last_job", "job_id": job_id, "label": "Готовлю отчёт",
            },
        )
    if job["status"] == "error":
        return _render_report_result(request, None, job["error"])
    return _render_report_result(request, job["result"])


@app.post("/reports/download")
def reports_download(
    source: str = Form("all"),
    days: float = Form(7),
    question: str = Form(""),
    tier: str = Form("flash"),
    provider: str = Form("gemini_direct"),
):
    try:
        answer, items, error = _build_report(source, days, question, tier, provider)
    except RouterAIError as e:
        answer, items, error = f"Ошибка вызова модели: {e}", [], None
    if error:
        answer, items = error, []
    clean_answer, chart = reporting.extract_chart(answer)
    html_body = "<p>" + reporting._linkify(clean_answer, items, "html").replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    html = reporting._render_html("Сводный отчёт", question or "(общая сводка)", html_body, items, chart=chart)
    filename = f"report_{datetime.now():%Y%m%d_%H%M}.html"
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ #
# Модуль 2 — база знаний
# ------------------------------------------------------------------ #

@app.get("/kb", response_class=HTMLResponse)
def kb_page(request: Request):
    stats = {"total": 0, "drive": 0, "discord": 0}
    if kb_index.INDEX_DB.exists():
        import sqlite3

        conn = sqlite3.connect(kb_index.INDEX_DB)
        stats["total"] = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        for source, count in conn.execute("SELECT source, COUNT(*) FROM kb_chunks GROUP BY source"):
            stats[source] = count
        conn.close()
    return templates.TemplateResponse(request, "kb.html", {"active": "kb", "stats": stats})


def _run_kb_job(question: str, provider: str) -> dict:
    client = get_client()
    if not kb_index.INDEX_DB.exists():
        return {"error": "Индекс пуст — запусти python kb_index.py build (см. README).", "answer_html": None}
    try:
        top_chunks = kb_index.search(question, client, top_k=6)
        if not top_chunks:
            return {"error": None, "answer_html": "В базе знаний пока ничего не проиндексировано."}
        answer = client.report(
            question=question, items=top_chunks, tier="flash", task_label="kb_answer",
            provider=provider or None,
        )
        answer_html = reporting._linkify(answer, top_chunks, "html")
        return {"error": None, "answer_html": answer_html}
    except RouterAIError as e:
        return {"error": f"Ошибка вызова модели: {e}", "answer_html": None}


@app.post("/kb/ask", response_class=HTMLResponse)
def kb_ask(request: Request, question: str = Form(...), provider: str = Form("gemini_direct")):
    job_id = jobs.create_job(_run_kb_job, question, provider)
    return templates.TemplateResponse(
        request, "_job_poll.html",
        {
            "poll_id": "kbJob", "poll_url": f"/kb/status/{job_id}",
            "storage_key": "kb_last_job", "job_id": job_id, "label": "Ищу и отвечаю",
        },
    )


@app.get("/kb/status/{job_id}", response_class=HTMLResponse)
def kb_status(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return _stale_job_response("kb_last_job")
    if job["status"] == "running":
        return templates.TemplateResponse(
            request, "_job_poll.html",
            {
                "poll_id": "kbJob", "poll_url": f"/kb/status/{job_id}",
                "storage_key": "kb_last_job", "job_id": job_id, "label": "Ищу и отвечаю",
            },
        )
    if job["status"] == "error":
        return templates.TemplateResponse(request, "kb_result.html", {"error": job["error"], "answer_html": None})
    return templates.TemplateResponse(request, "kb_result.html", job["result"])


# ------------------------------------------------------------------ #
# Модуль 3 — Discord + мониторинг
# ------------------------------------------------------------------ #

_COMPLEX_IGNORE_PATH = Path(__file__).resolve().parent / "complex_ignore.yaml"


def _load_complex_ignore_list() -> list[str]:
    if not _COMPLEX_IGNORE_PATH.exists():
        return []
    try:
        data = yaml.safe_load(_COMPLEX_IGNORE_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    return [str(x) for x in (data.get("ignored_sales_point_ids") or [])]


def _complex_table_context() -> dict:
    """Общие данные для таблицы комплексов — используются и при обычном
    рендере /discord, и при обновлении панели после AI-анализа (не
    дублируем sales_points внутри job-результата, просто пересобираем)."""
    sales_points = sorted(ds.load_sales_points(), key=lambda sp: sp.get("updated_at") or "", reverse=True)
    ignored = set(_load_complex_ignore_list())
    if ignored:
        sales_points = [sp for sp in sales_points if sp.get("sales_point_id") not in ignored]
    sales_points = sales_points[:100]

    analysis = complex_analysis.load_cache()
    results = analysis.get("results", {})
    for sp in sales_points:
        info = results.get(sp.get("sales_point_id"), {})
        sp["ai_level"] = info.get("level")
        sp["ai_reason"] = info.get("reason", "")

    return {
        "sales_points": sales_points,
        "analysis_generated_at": analysis.get("generated_at"),
        "analysis_errors": analysis.get("errors") or [],
    }


@app.get("/discord", response_class=HTMLResponse)
def discord_page(request: Request):
    channels = ds.list_discord_channels()
    fibbee_tickets = reporting._load_fibbee(datetime.now(timezone.utc) - timedelta(days=7))[:40]
    return templates.TemplateResponse(
        request,
        "discord.html",
        {
            "active": "discord",
            "channels": channels,
            "fibbee_tickets": fibbee_tickets,
            **_complex_table_context(),
        },
    )


def _run_complex_analysis_job(provider: str) -> dict:
    sales_points = _complex_table_context()["sales_points"]
    return complex_analysis.run_analysis(get_client(), sales_points, provider=provider)


@app.post("/discord/analyze", response_class=HTMLResponse)
def discord_analyze(request: Request, provider: str = Form("gemini_direct")):
    job_id = jobs.create_job(_run_complex_analysis_job, provider)
    return templates.TemplateResponse(
        request, "_job_poll.html",
        {
            "poll_id": "complexAnalysisJob", "poll_url": f"/discord/analyze/status/{job_id}",
            "storage_key": "complex_analysis_last_job", "job_id": job_id,
            "label": "Анализирую комплексы (тикеты + доля неудачных заказов)",
        },
    )


@app.get("/discord/analyze/status/{job_id}", response_class=HTMLResponse)
def discord_analyze_status(request: Request, job_id: str):
    # Эта панель свапается через hx-swap="outerHTML" (см. _complex_table.html,
    # id="complexPanel") — в отличие от остальных *_status-эндпоинтов нельзя
    # просто отдать нейтральную заметку без id="complexPanel": outerHTML-свап
    # заменил бы всю панель (кнопку "Проанализировать сейчас" и таблицу тоже)
    # заметкой без возможности вернуть их без полной перезагрузки страницы.
    # Поэтому и при "не найдено", и при ошибке — всегда перерендериваем
    # актуальную панель целиком (те же данные, что при обычной загрузке
    # /discord), просто с баннером сверху при необходимости.
    job = jobs.get_job(job_id)
    panel_html = templates.TemplateResponse(request, "_complex_table.html", _complex_table_context()).body.decode("utf-8")
    if job is None:
        return HTMLResponse(f'{panel_html}<script>localStorage.removeItem("complex_analysis_last_job");</script>')
    if job["status"] == "running":
        return templates.TemplateResponse(
            request, "_job_poll.html",
            {
                "poll_id": "complexAnalysisJob", "poll_url": f"/discord/analyze/status/{job_id}",
                "storage_key": "complex_analysis_last_job", "job_id": job_id,
                "label": "Анализирую комплексы (тикеты + доля неудачных заказов)",
            },
        )
    if job["status"] == "error":
        banner = f'<p class="muted" style="color:var(--danger)">Ошибка анализа: {job["error"]}</p>'
        return HTMLResponse(banner + panel_html)
    return HTMLResponse(panel_html)


@app.get("/discord/messages", response_class=HTMLResponse)
def discord_messages(request: Request, hours: float = 24, channel: str = ""):
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    msgs = ds.load_discord_messages(since.isoformat(), until.isoformat(), channel or None, limit=500)
    for m in msgs:
        m["highlight"] = highlight.classify(m["content"])
        m["url"] = ds.discord_channel_url(m["guild_id"], m["channel_id"], m["message_id"])
    return templates.TemplateResponse(request, "discord_messages.html", {"messages": msgs})


def _run_discord_summary_job(hours: float, channel: str, provider: str, topic: str) -> dict:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    msgs = ds.load_discord_messages(since.isoformat(), until.isoformat(), channel or None, limit=2000)
    if not msgs:
        return {"summary": None, "error": "Нет сообщений за период.", "count": 0}

    client = get_client()
    lines = [f"[{m['created_at']}] #{m['channel_name']} {m['author_name']}: {m['content']}" for m in msgs]
    topic_note = f" Особое внимание удели теме: {topic.strip()}." if topic and topic.strip() else ""
    instructions = (
        "Сделай сводку по сообщениям Discord за период. Отдельно выдели: инциденты/проблемы, "
        "упоминания monitoring/service team, открытые вопросы без ответа."
        f"{topic_note} По-русски, структурированно."
    )
    provider = provider or None  # nano/flash — оба совместимы и с gemini_direct, и с openrouter
    try:
        # map-reduce при большом объёме, чтобы не упереться в контекст/бюджет
        window = 60
        if len(lines) <= window:
            summary = client.summarize(
                "\n".join(lines), instructions=instructions, tier="flash",
                task_label="discord_summary", provider=provider,
            )
        else:
            partials = []
            map_instructions = "Кратко выдели факты, инциденты и открытые вопросы."
            if topic_note:
                map_instructions += topic_note
            for i in range(0, len(lines), window):
                chunk = "\n".join(lines[i : i + window])
                partials.append(
                    client.summarize(
                        chunk, instructions=map_instructions,
                        tier="nano", task_label="discord_summary_map", provider=provider,
                        retries=4,  # map-стадия шлёт много nano-вызовов подряд — упирались в 429
                    )
                )
            combined = "\n\n".join(partials)
            summary = client.summarize(
                combined, instructions=instructions, tier="flash",
                task_label="discord_summary_reduce", provider=provider,
            )
    except RouterAIError as e:
        return {"summary": None, "error": str(e), "count": len(msgs)}

    return {"summary": summary, "error": None, "count": len(msgs)}


@app.post("/discord/summarize", response_class=HTMLResponse)
def discord_summarize(
    request: Request,
    hours: float = Form(24),
    channel: str = Form(""),
    provider: str = Form("gemini_direct"),
    topic: str = Form(""),
):
    job_id = jobs.create_job(_run_discord_summary_job, hours, channel, provider, topic)
    return templates.TemplateResponse(
        request, "_job_poll.html",
        {
            "poll_id": "discordSummaryJob", "poll_url": f"/discord/summarize/status/{job_id}",
            "storage_key": "discord_summary_last_job", "job_id": job_id, "label": "Обобщаю период",
        },
    )


@app.get("/discord/summarize/status/{job_id}", response_class=HTMLResponse)
def discord_summarize_status(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(
            request, "discord_summary.html",
            {"summary": None, "error": "Задача не найдена (веб-морда перезапускалась?) — запроси заново."},
        )
    if job["status"] == "running":
        return templates.TemplateResponse(
            request, "_job_poll.html",
            {
                "poll_id": "discordSummaryJob", "poll_url": f"/discord/summarize/status/{job_id}",
                "storage_key": "discord_summary_last_job", "job_id": job_id, "label": "Обобщаю период",
            },
        )
    if job["status"] == "error":
        return templates.TemplateResponse(request, "discord_summary.html", {"summary": None, "error": job["error"]})
    return templates.TemplateResponse(request, "discord_summary.html", job["result"])


# ------------------------------------------------------------------ #
# Учёт токенов/расходов
# ------------------------------------------------------------------ #

@app.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request):
    import json

    stats = usage_stats.read_usage()
    key_info, key_error = None, None
    try:
        key_info = get_client().key_info()
    except RouterAIError as e:
        key_error = str(e)

    chart_json = json.dumps(
        {
            "labels": [d["date"] for d in stats["daily"]],
            "tokens": [d["tokens"] for d in stats["daily"]],
            "cost": [round(d["cost"], 4) for d in stats["daily"]],
        }
    )
    health = health_monitor.read_status()
    return templates.TemplateResponse(
        request,
        "usage.html",
        {
            "active": "usage",
            "stats": stats,
            "key_info": key_info,
            "key_error": key_error,
            "chart_json": chart_json,
            "health": health,
        },
    )


@app.get("/discord/dashboard/{sales_point_id}")
def fibbee_dashboard(sales_point_id: str):
    """
    Минтит ссылку на нативный дашборд комплекса через FibbeeClient (его
    собственный venv — не тащим curl_cffi в зависимости веб-морды ради
    одной ссылки) и редиректит браузер пользователя туда.
    """
    python = FIBBEE_DIR / "venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = FIBBEE_DIR / "venv" / "bin" / "python"
    try:
        proc = subprocess.run(
            [str(python), "dashboard_url.py", sales_point_id],
            cwd=str(FIBBEE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        return HTMLResponse(f"Не удалось получить ссылку на дашборд: {e}", status_code=500)

    out = proc.stdout.strip()
    if proc.returncode != 0 or not out.startswith("http"):
        return HTMLResponse(f"Не удалось получить ссылку на дашборд: {out or proc.stderr}", status_code=500)
    return RedirectResponse(out)
