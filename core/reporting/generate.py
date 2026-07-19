"""
Сводные отчёты по заявкам/тикетам/задачам через router_ai.

Источники: Service Desk (заявки), Fibbee ERP (тикеты), Tracker (задачи),
Discord (сообщения, сгруппированные по каналу+дню) — читает их SQLite
напрямую (без импорта коннекторов — у каждого свои, несовместимые
зависимости в отдельных venv, а тут нужен только SELECT).

Ссылки на источники строятся по link_templates.yaml — шаблоны там ДОГАДКА
по структуре API, не подтверждены в браузере. См. комментарий в этом файле.

Запуск (venv — тот же, что у core/router_ai, см. README):
    python generate.py --source all --days 7 --format html
    python generate.py --source servicedesk --days 1 --question "Что случилось за сутки?"
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router_ai"))
from client import RouterAIClient, RouterAIError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LINK_TEMPLATES = yaml.safe_load(
    (Path(__file__).resolve().parent / "link_templates.yaml").read_text(encoding="utf-8")
)
REPORTS_OUT_DIR = ROOT / "reports"  # gitignored (реальные операционные данные)

MAX_ITEMS_DEFAULT = 60


def _parse_dt(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit() and len(s) >= 12:  # epoch millis (Fibbee)
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:  # ISO 8601 (Tracker, вероятно и SD)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_servicedesk(since, limit=500, open_only=False):
    """open_only=True — только незакрытые заявки (status != 'Закрыта'), без
    ограничения по времени создания (для общей таблицы "открытые сейчас",
    см. connectors/sheets_export — старая, но ещё не закрытая заявка обязана
    остаться видна, а не выпасть из-за LIMIT/сортировки по свежести)."""
    db = ROOT / "connectors" / "servicedesk" / "data" / "servicedesk.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    query = (
        "SELECT code, title, client, object, status, engineer, priority, severity, "
        "description, diagnosis, work_result, failure_reason, created_at, closed_at "
        "FROM servicedesk_tickets"
    )
    params = []
    if open_only:
        query += " WHERE status != 'Закрыта'"
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    items = []
    for r in rows:
        dt = _parse_dt(r["created_at"])
        if since and dt and dt < since:
            continue
        text = " | ".join(filter(None, [r["description"], r["diagnosis"], r["work_result"], r["failure_reason"]]))
        items.append(
            {
                "ref": r["code"] or "SD-?",
                "source": "Service Desk",
                "title": r["title"] or "",
                "status": r["status"] or "",
                "url": LINK_TEMPLATES.get("servicedesk", "").format(code=r["code"] or ""),
                "text": (
                    f"клиент={r['client']}, объект={r['object']}, инженер={r['engineer']}, "
                    f"приоритет={r['priority']}, критичность={r['severity']}. {text}"
                ),
                "created_at": r["created_at"],
            }
        )
    return items


def _load_fibbee(since, limit=500, open_only=False):
    """open_only=True — только незакрытые тикеты (state='open'; 'closed' и
    'closed-immediately' считаются закрытыми), без ограничения по времени —
    см. _load_servicedesk выше про то же самое соображение."""
    db = ROOT / "connectors" / "fibbee" / "data" / "fibbee.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    query = (
        "SELECT ticket_id, number, node, zone, status, state, category, priority, "
        "description, created_at, updated_at FROM fibbee_tickets"
    )
    params = []
    if open_only:
        query += " WHERE state = 'open'"
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    items = []
    for r in rows:
        dt = _parse_dt(r["created_at"])
        if since and dt and dt < since:
            continue
        items.append(
            {
                "ref": f"#{r['number']}" if r["number"] is not None else r["ticket_id"],
                "source": "Fibbee ERP",
                "title": f"{r['node']} / {r['category']}" if r["node"] else (r["category"] or ""),
                "status": f"{r['status']} ({r['state']})",
                "url": LINK_TEMPLATES.get("fibbee", "").format(
                    ticket_id=r["ticket_id"], number=r["number"] or ""
                ),
                "text": f"зона={r['zone']}, приоритет={r['priority']}. {r['description'] or ''}",
                "created_at": r["created_at"],
            }
        )
    return items


def _load_tracker(since, limit=500):
    db = ROOT / "connectors" / "tracker" / "data" / "tracker.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT code, task_type_code, title, description, status_name, assignee_name, is_urgent, "
        "created_at, updated_at FROM tracker_tasks ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        dt = _parse_dt(r["created_at"])
        if since and dt and dt < since:
            continue
        items.append(
            {
                "ref": r["code"] or "?",
                "source": "Tracker",
                "title": r["title"] or "",
                "status": r["status_name"] or "",
                "url": LINK_TEMPLATES.get("tracker", "").format(
                    code=r["code"] or "", type=r["task_type_code"] or ""
                ),
                "text": (
                    f"исполнитель={r['assignee_name']}, срочно={'да' if r['is_urgent'] else 'нет'}. "
                    f"{r['description'] or ''}"
                ),
                "created_at": r["created_at"],
            }
        )
    return items


_MSK = timezone(timedelta(hours=3))  # без перехода на летнее — Россия его не использует
_MENTION_RE = re.compile(r"<@!?(\d+)>")  # только пользовательские упоминания, не <@&ID> (роли)


def discord_author_map() -> dict:
    """
    author_id -> последнее известное отображаемое имя.

    Отдельной таблицы участников сервера у нас нет — но author_name уже
    сохраняется на каждом сообщении (connectors/discord/bot.py), поэтому имя
    для упоминания можно взять из истории сообщений самого автора (последнее
    по времени, на случай смены ника). Не резолвит упоминания людей, которые
    сами никогда не писали в отслеживаемых каналах — для них останется
    <@ID> как есть.
    """
    db = ROOT / "connectors" / "discord" / "data" / "discord.db"
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT author_id, author_name, MAX(created_at) FROM discord_messages "
            "WHERE author_id IS NOT NULL GROUP BY author_id"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows if r[1]}


def resolve_discord_mentions(content: str, author_map: dict) -> str:
    """<@123456789012345678> -> @Имя. Роли (<@&ID>) не трогает — их подсветкой
    по ID из monitoring_config.yaml занимается core/webapp/highlight.py."""
    if not content or "<@" not in content:
        return content

    def _repl(m):
        name = author_map.get(m.group(1))
        return f"@{name}" if name else m.group(0)

    return _MENTION_RE.sub(_repl, content)


def format_discord_dt(iso_str) -> str:
    """ISO 8601 с микросекундами и +00:00 (как их пишет connectors/discord/bot.py)
    -> "ДД.ММ.ГГГГ ЧЧ:ММ" по московскому времени — читаемо в UI и в отчётах."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str))
    except ValueError:
        return str(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")


def _load_discord(since):
    db = ROOT / "connectors" / "discord" / "data" / "discord.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    query = (
        "SELECT guild_id, channel_id, channel_name, author_name, content, created_at "
        "FROM discord_messages WHERE deleted_at IS NULL AND content != ''"
    )
    params = []
    if since:
        query += " AND created_at >= ?"
        params.append(since.isoformat())
    query += " ORDER BY channel_id, created_at"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    # группировка по каналу+дню — иначе сырые сообщения (их на порядки больше,
    # чем заявок) задавят остальные источники при общей сортировке по свежести
    groups: dict[tuple, list] = {}
    for r in rows:
        day = r["created_at"][:10]
        groups.setdefault((r["channel_id"], r["channel_name"], day, r["guild_id"]), []).append(r)

    author_map = discord_author_map()
    items = []
    for (channel_id, channel_name, day, guild_id), msgs in groups.items():
        transcript = "\n".join(
            f"{m['author_name']}: {resolve_discord_mentions(m['content'], author_map)}" for m in msgs
        )
        items.append(
            {
                "ref": f"#{channel_name}, {day}",
                "source": "Discord",
                "title": f"#{channel_name}",
                "status": f"{len(msgs)} сообщений",
                "url": f"https://discord.com/channels/{guild_id}/{channel_id}",
                "text": transcript[:2000],
                "created_at": f"{day}T23:59:59+00:00",
            }
        )
    return items


LOADERS = {
    "servicedesk": _load_servicedesk,
    "fibbee": _load_fibbee,
    "tracker": _load_tracker,
    "discord": _load_discord,
}


_CHART_RE = re.compile(r"```chart\s*\n(.*?)\n```", re.DOTALL)


def extract_chart(text: str) -> tuple[str, dict | None]:
    """
    Ищет один блок ```chart ... ``` (см. промпт report() в client.py),
    вырезает его из текста и парсит как JSON. Если блока нет или JSON битый
    (модель имеет право ошибиться в формате) — возвращает текст как есть и
    None, без падения.
    """
    m = _CHART_RE.search(text)
    if not m:
        return text, None
    clean_text = (text[: m.start()] + text[m.end():]).strip()
    try:
        chart = json.loads(m.group(1))
    except json.JSONDecodeError:
        return text, None
    return clean_text, chart


def _linkify(text: str, items: list, fmt: str) -> str:
    def repl(m):
        idx = int(m.group(1))
        if 1 <= idx <= len(items):
            it = items[idx - 1]
            label = f"{it['source']} {it['ref']}"
            if fmt == "html":
                return f'<a href="{it["url"]}" target="_blank">[{idx}] {label}</a>'
            return f"[{label}]({it['url']})"
        return m.group(0)

    return re.sub(r"\[(\d+)\]", repl, text)


def _render_html(title: str, question: str, body_html: str, items: list, chart: dict | None = None) -> str:
    rows = "\n".join(
        f'<tr><td>{i}</td><td>{it["source"]}</td>'
        f'<td><a href="{it["url"]}" target="_blank">{it["ref"]}</a></td>'
        f'<td>{it["status"]}</td><td>{it["title"]}</td></tr>'
        for i, it in enumerate(items, start=1)
    )
    chart_html = ""
    if chart:
        chart_html = (
            '<canvas id="reportChart" style="max-width:800px"></canvas>'
            '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'
            "<script>new Chart(document.getElementById('reportChart'), "
            f"{{type: {json.dumps(chart.get('type', 'bar'))}, "
            f"data: {{labels: {json.dumps(chart.get('labels', []))}, "
            f"datasets: {json.dumps(chart.get('datasets', []))}}}, "
            f"options: {{plugins: {{title: {{display: true, text: {json.dumps(chart.get('title', ''))}}}}}}}}});"
            "</script>"
        )
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
h1 {{ font-size: 1.4rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1.5rem; font-size: 0.9rem; }}
th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
th {{ background: #f5f5f5; }}
.question {{ color: #555; font-style: italic; margin-bottom: 1rem; }}
a {{ color: #0645ad; }}
</style></head><body>
<h1>{title}</h1>
<p class="question">{question}</p>
<div>{body_html}</div>
{chart_html}
<h2>Источники ({len(items)})</h2>
<table><tr><th>#</th><th>Источник</th><th>Ref</th><th>Статус</th><th>Заголовок</th></tr>
{rows}
</table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Сводный отчёт по заявкам/тикетам/задачам")
    parser.add_argument("--source", choices=["all", "servicedesk", "fibbee", "tracker", "discord"], default="all")
    parser.add_argument("--days", type=float, default=7, help="глубина периода в днях (0 = без фильтра по дате)")
    parser.add_argument("--question", default=None, help="что спросить у модели; по умолчанию — общая сводка")
    parser.add_argument("--format", choices=["text", "html"], default="text")
    parser.add_argument("--out", default=None, help="путь для HTML-файла (по умолчанию reports/report_<дата>.html)")
    parser.add_argument("--tier", default="flash", choices=["nano", "flash", "pro"])
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS_DEFAULT)
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days > 0 else None

    sources = list(LOADERS.keys()) if args.source == "all" else [args.source]
    items = []
    for s in sources:
        items.extend(LOADERS[s](since))

    if not items:
        print("Нет данных за выбранный период (проверь, что sync.py по нужным коннекторам запускался).")
        sys.exit(1)

    items.sort(key=lambda it: it.get("created_at") or "", reverse=True)
    truncated_note = ""
    if len(items) > args.max_items:
        truncated_note = (
            f"\n\n_(в анализ включены {args.max_items} самых свежих из {len(items)} найденных за период — "
            f"сузь --days или --source, чтобы разобрать остальное)_"
        )
        items = items[: args.max_items]

    question = args.question or (
        f"Сделай сводку за последние {args.days} дн. по источникам {', '.join(sources)}: "
        f"сколько всего, разбивка по статусам, повторяющиеся проблемы/узлы, что требует "
        f"внимания в первую очередь."
    )

    try:
        client = RouterAIClient()
    except RouterAIError as e:
        print(f"Ошибка конфигурации router_ai: {e}")
        sys.exit(1)

    try:
        answer = client.report(question=question, items=items, tier=args.tier, task_label="report")
    except RouterAIError as e:
        print(f"Ошибка вызова модели: {e}")
        sys.exit(1)

    answer += truncated_note

    clean_answer, chart = extract_chart(answer)

    if args.format == "text":
        print(_linkify(clean_answer, items, "text"))
        if chart:
            print("\n(модель предложила график — доступен только при --format html)")
    else:
        html_body = "<p>" + _linkify(clean_answer, items, "html").replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
        html = _render_html("Сводный отчёт", question, html_body, items, chart=chart)
        REPORTS_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = Path(args.out) if args.out else REPORTS_OUT_DIR / f"report_{datetime.now():%Y%m%d_%H%M}.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"Сохранено: {out_path}")


if __name__ == "__main__":
    main()
