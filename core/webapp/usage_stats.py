"""
Учёт расхода токенов/денег (по просьбе пользователя: "нужно отслеживать
сколько токенов израсходовано и сколько ещё осталось, возможно показывать
стоимость запроса и график потребления").

Два источника данных:
- баланс/лимит ключа OpenRouter — GET /api/v1/key через
  RouterAIClient.key_info(), см. core/router_ai/client.py (бесплатный
  запрос, токены не тратит; к прямому Gemini не относится — у него нет
  аналога, это бесплатный тир без баланса);
- сама история расхода — logs/router_ai_usage.csv, куда client.py пишет
  строку на каждый вызов (chat/embed), включая provider, cost (0 у
  gemini_direct — бесплатный тир) и модель.

2026-07-19: добавлена разбивка по provider (openrouter vs gemini_direct) —
после перевода nano/flash на корпоративный Gemini это и есть ответ на "куда
уходят токены/деньги": видно долю вызовов, которые теперь ничего не стоят.
estimated_savings_usd — грубая оценка "сколько бы стоили вызовы gemini_direct,
если бы шли через OpenRouter", по среднему $/токен той же модели из
исторических строк с provider=openrouter. Если для какой-то модели такой
истории нет (ещё не было ни одного платного вызова) — вклад этой модели в
оценку равен 0, а не выдумка; общая оценка тогда только частичная.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USAGE_LOG_PATH = ROOT / "logs" / "router_ai_usage.csv"


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_usage() -> dict:
    """
    Возвращает {"daily": [...], "by_task": [...], "by_model": [...],
    "by_provider": [{provider, tokens, cost, calls}], "total_tokens",
    "total_cost", "total_calls", "cost_known", "estimated_savings_usd"}.
    cost_known=False, если провайдер вообще ни разу не вернул cost (тогда
    график по деньгам рисовать нечего, только токены).
    """
    empty = {
        "daily": [], "by_task": [], "by_model": [], "by_provider": [],
        "total_tokens": 0, "total_cost": 0.0, "total_calls": 0, "cost_known": False,
        "estimated_savings_usd": 0.0,
    }
    if not USAGE_LOG_PATH.exists():
        return empty

    with open(USAGE_LOG_PATH, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return empty

    # Средняя $/токен по каждой модели, только по исторически платным
    # (openrouter) строкам с известной ценой — база для оценки экономии
    # на бесплатных gemini_direct-вызовах той же модели.
    paid_tokens_by_model = defaultdict(int)
    paid_cost_by_model = defaultdict(float)
    for row in rows:
        if (row.get("provider") or "openrouter") == "openrouter" and row.get("cost_usd"):
            model = row.get("model") or "?"
            paid_tokens_by_model[model] += _to_int(row.get("total_tokens"))
            paid_cost_by_model[model] += _to_float(row.get("cost_usd"))
    avg_cost_per_token = {
        m: (paid_cost_by_model[m] / paid_tokens_by_model[m]) if paid_tokens_by_model[m] else 0.0
        for m in paid_tokens_by_model
    }

    daily = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0})
    by_task = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0})
    by_model = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0})
    by_provider = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0})
    total_tokens = 0
    total_cost = 0.0
    total_calls = 0
    cost_known = False
    estimated_savings = 0.0

    for row in rows:
        day = (row.get("timestamp") or "")[:10]
        tokens = _to_int(row.get("total_tokens"))
        cost = _to_float(row.get("cost_usd"))
        if row.get("cost_usd"):
            cost_known = True
        task = row.get("task_label") or "?"
        model = row.get("model") or "?"
        provider = row.get("provider") or "openrouter"  # старые строки до миграции — считаем openrouter

        daily[day]["tokens"] += tokens
        daily[day]["cost"] += cost
        daily[day]["calls"] += 1
        by_task[task]["tokens"] += tokens
        by_task[task]["cost"] += cost
        by_task[task]["calls"] += 1
        by_model[model]["tokens"] += tokens
        by_model[model]["cost"] += cost
        by_model[model]["calls"] += 1
        by_provider[provider]["tokens"] += tokens
        by_provider[provider]["cost"] += cost
        by_provider[provider]["calls"] += 1
        total_tokens += tokens
        total_cost += cost
        total_calls += 1

        if provider != "openrouter":
            estimated_savings += tokens * avg_cost_per_token.get(model, 0.0)

    return {
        "daily": [{"date": d, **v} for d, v in sorted(daily.items())],
        "by_task": sorted(
            [{"task": k, **v} for k, v in by_task.items()], key=lambda x: x["tokens"], reverse=True
        ),
        "by_model": sorted(
            [{"model": k, **v} for k, v in by_model.items()], key=lambda x: x["tokens"], reverse=True
        ),
        "by_provider": sorted(
            [{"provider": k, **v} for k, v in by_provider.items()], key=lambda x: x["tokens"], reverse=True
        ),
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "total_calls": total_calls,
        "cost_known": cost_known,
        "estimated_savings_usd": estimated_savings,
    }
