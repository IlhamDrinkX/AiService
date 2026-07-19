"""
Единая точка выхода в LLM/эмбеддинги (router_ai_client).

Все остальные модули (модуль задач, база знаний, Discord-сводки, отчёты)
дергают только этот клиент и никогда не хардкодят имя модели, провайдера или
URL у себя — это даёт возможность сменить модель/уровень/провайдера, поменяв
только models.yaml/.env, без правки модулей (см. functional_plan_ui.md, §4
и architecture_plan.md, §5).

Два провайдера на выбор, независимо для каждого уровня (models.yaml,
tiers.<tier>.provider):
- "openrouter" (по умолчанию, если provider не указан) — платно, любая модель
  каталога OpenRouter;
- "gemini_direct" — прямой вызов Google Generative Language API по
  корпоративному ключу (GEMINI_API_KEY в .env), бесплатный тир, без OpenRouter
  и без его комиссии. Добавлен 2026-07-19 для экономии — nano/flash переведены
  на него, pro (Claude) и эмбеддинги (OpenAI) остались на OpenRouter, т.к.
  бесплатный Gemini API их не покрывает.

Три уровня сложности + эмбеддинги, конфиг — в models.yaml. Уровень "без LLM"
(подсветка упоминаний, diff обновлений) сюда не входит — реализуется прямым
кодом в соответствующих модулях.

Каждый вызов логируется в logs/router_ai_usage.csv (провайдер, модель,
уровень, токены, стоимость если провайдер её вернул, метка задачи) — так
видно, где реально уходят деньги/бесплатная квота, и можно пересмотреть уровни.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent.parent

# Явный путь, а не просто load_dotenv() — тот ищет .env в текущей рабочей
# директории процесса, а не рядом с этим файлом. Ломается, если этот модуль
# импортируют из другого cwd (core/webapp, core/reporting) — именно так и
# происходит, оба это делают.
load_dotenv(ROOT / ".env")
CONFIG_PATH = ROOT / "models.yaml"
USAGE_LOG_PATH = PROJECT_ROOT / "logs" / "router_ai_usage.csv"
USAGE_LOG_FIELDS = [
    "timestamp", "task_label", "tier", "provider", "model",
    "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "elapsed_s",
]

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# Опционально, но OpenRouter учитывает их в рейтингах/квотах провайдеров —
# не обязательны для работы.
APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "AI Service System")
APP_URL = os.environ.get("OPENROUTER_APP_URL", "")

# Корпоративный Gemini напрямую через Google Generative Language API —
# см. models.yaml, tiers.<tier>.provider: gemini_direct. Ключ берётся на
# aistudio.google.com (или из GCP-проекта, где включён Generative Language
# API), в наш биллинг GCP-проекта ("sheets") не завязан.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.environ.get(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")

DEFAULT_TIMEOUT = 60


class RouterAIError(RuntimeError):
    pass


def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ensure_usage_log_schema() -> None:
    """Мигрирует старый usage.csv (записан до 2026-07-19, без колонки
    provider — тогда был только OpenRouter) на новую схему: вставляет
    provider="openrouter" во все прежние строки. Без этого csv.DictReader в
    usage_stats.py читает новые/старые строки не совпадающими по колонкам.
    Идемпотентно и почти бесплатно (одна проверка заголовка) — гонять на
    каждый вызов ok, файл небольшой (человекочитаемый лог, не БД)."""
    if not USAGE_LOG_PATH.exists():
        return
    with open(USAGE_LOG_PATH, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows or "provider" in rows[0]:
        return
    old_header = rows[0]
    try:
        model_idx = old_header.index("model")
    except ValueError:
        return  # неожиданный формат — не трогаем, лучше кривой лог, чем потеря данных
    migrated = [USAGE_LOG_FIELDS]
    for row in rows[1:]:
        row = list(row)
        row.insert(model_idx, "openrouter")
        migrated.append(row)
    with open(USAGE_LOG_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(migrated)


class RouterAIClient:
    def __init__(
        self,
        api_key: str | None = None,
        config_path: Path = CONFIG_PATH,
        gemini_api_key: str | None = None,
    ):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        # OpenRouter нужен только если хоть один настроенный уровень/эмбеддинг
        # реально на нём висит — не требуем ключ, если весь конфиг переведён
        # на gemini_direct.
        self.api_key = api_key or API_KEY
        needs_openrouter = any(
            (t or {}).get("provider", "openrouter") == "openrouter"
            for t in self.config.get("tiers", {}).values()
        ) or self.config.get("embedding")
        if needs_openrouter and not self.api_key:
            raise RouterAIError(
                "OPENROUTER_API_KEY не задан — скопируй .env.example в .env и вставь ключ "
                "(нужен как минимум одному уровню/эмбеддингам в models.yaml)"
            )
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )
            if APP_TITLE:
                self._session.headers["X-Title"] = APP_TITLE
            if APP_URL:
                self._session.headers["HTTP-Referer"] = APP_URL

        # Прямой Gemini — свой ключ, своя сессия (отдельная авторизация,
        # header x-goog-api-key, а не Bearer).
        self.gemini_api_key = gemini_api_key or GEMINI_API_KEY
        self._gemini_session = requests.Session()
        if self.gemini_api_key:
            self._gemini_session.headers.update(
                {
                    "x-goog-api-key": self.gemini_api_key,
                    "Content-Type": "application/json",
                }
            )

        _ensure_usage_log_schema()

    # ------------------------------------------------------------------ #
    # низкоуровневые вызовы
    # ------------------------------------------------------------------ #

    def _tier_config(self, tier: str) -> dict:
        tiers = self.config.get("tiers", {})
        if tier not in tiers:
            raise RouterAIError(f"неизвестный уровень '{tier}', доступны: {list(tiers)}")
        return tiers[tier]

    def chat(
        self,
        messages: list[dict],
        tier: str = "flash",
        task_label: str = "chat",
        retries: int = 2,
        **overrides,
    ) -> dict:
        """Низкоуровневый вызов чата. Возвращает {"text", "usage", "raw"}.
        Провайдер берётся из models.yaml (tiers.<tier>.provider), не из
        вызывающего кода — переключение уровня между OpenRouter и gemini_direct
        не требует правки модулей, только конфига."""
        tier_cfg = self._tier_config(tier)
        provider = overrides.pop("provider", tier_cfg.get("provider", "openrouter"))
        # Имя модели зависит от ФАКТИЧЕСКОГО провайдера, не только от того,
        # что в model_config по умолчанию — алиасы вроде gemini-flash-latest
        # существуют только в прямом Gemini API, а не в каталоге OpenRouter
        # (и наоборот, у OpenRouter свои версии-даты в имени). Если
        # пользователь явно переключил провайдера через UI (см. reports/kb/
        # discord-сводки — там есть выбор "Провайдер"), но в конфиге для
        # этого уровня нет отдельного имени под другого провайдера — берём
        # tier_cfg["model"] как раньше и надеемся, что оно валидно и там
        # (было так, пока не поймали живьём HTTP 400 "not a valid model ID").
        if "model" in overrides:
            model = overrides.pop("model")
        elif provider == "openrouter" and tier_cfg.get("openrouter_model"):
            model = tier_cfg["openrouter_model"]
        elif provider == "gemini_direct" and tier_cfg.get("gemini_model"):
            model = tier_cfg["gemini_model"]
        else:
            model = tier_cfg["model"]
        max_tokens = overrides.pop("max_tokens", tier_cfg.get("max_tokens", 1000))
        temperature = overrides.pop("temperature", tier_cfg.get("temperature", 0.3))

        if provider == "gemini_direct":
            result = self._chat_gemini_direct(
                messages, model=model, max_tokens=max_tokens, temperature=temperature,
                retries=retries, **overrides,
            )
        elif provider == "openrouter":
            result = self._chat_openrouter(
                messages, model=model, max_tokens=max_tokens, temperature=temperature,
                retries=retries, **overrides,
            )
        else:
            raise RouterAIError(f"неизвестный provider '{provider}' у уровня '{tier}'")

        self._log_usage(
            task_label=task_label, tier=tier, provider=provider, model=model,
            usage=result["usage"], elapsed=result["elapsed"],
        )
        return {"text": result["text"], "usage": result["usage"], "raw": result["raw"]}

    def _chat_openrouter(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float,
        retries: int, **overrides,
    ) -> dict:
        if not self.api_key:
            raise RouterAIError(
                "OPENROUTER_API_KEY не задан, а уровень настроен на provider: openrouter"
            )
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "usage": {"include": True},
        }
        payload.update(overrides)

        last_err = None
        for attempt in range(1, retries + 1):
            started = time.monotonic()
            try:
                resp = self._session.post(
                    f"{BASE_URL}/chat/completions", json=payload, timeout=DEFAULT_TIMEOUT
                )
            except requests.RequestException as e:
                last_err = e
                continue
            elapsed = time.monotonic() - started

            if resp.status_code == 429 and attempt < retries:
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 400:
                last_err = RouterAIError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                if resp.status_code in (401, 402):
                    break  # ключ невалиден / кончились кредиты — повтор не поможет
                continue

            data = resp.json()
            choice = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {}) or {}
            return {"text": choice, "usage": usage, "raw": data, "elapsed": elapsed}

        raise RouterAIError(f"_chat_openrouter() не удался после {retries} попыток: {last_err}")

    def _chat_gemini_direct(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float,
        retries: int, **overrides,
    ) -> dict:
        """Прямой вызов Google Generative Language API (generateContent).
        Формат запроса/ответа у Gemini другой, чем OpenAI-совместимый —
        конвертируем messages -> contents/systemInstruction и usageMetadata ->
        общий формат usage (prompt_tokens/completion_tokens/total_tokens/cost),
        чтобы _log_usage и остальной код client.py не знали о разнице."""
        if not self.gemini_api_key:
            raise RouterAIError(
                "GEMINI_API_KEY не задан, а уровень настроен на provider: gemini_direct"
            )
        # "google/gemini-2.5-flash" (имя как в OpenRouter, для единообразия
        # в models.yaml/статистике) -> "gemini-2.5-flash" (имя модели в самом
        # Gemini API).
        gemini_model = model.split("/", 1)[-1]

        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": m.get("content", "")}],
            }
            for m in messages
            if m.get("role") != "system"
        ]
        payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        payload.update(overrides)

        url = f"{GEMINI_BASE_URL}/models/{gemini_model}:generateContent"
        last_err = None
        for attempt in range(1, retries + 1):
            started = time.monotonic()
            try:
                resp = self._gemini_session.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as e:
                last_err = e
                continue
            elapsed = time.monotonic() - started

            if resp.status_code == 429 and attempt < retries:
                # 429 у бесплатного тира Gemini — реально пойманный лимит
                # (например map-стадия суммаризации Discord шлёт много
                # nano-вызовов подряд): "Quota exceeded ... limit: 15 ...
                # Please retry in 42.7s". Фиксированные 3/6 секунд слишком
                # короткие для такого лимита — сервер прямо говорит, сколько
                # реально ждать, используем эту цифру, если она есть в
                # ответе (иначе как раньше, 3с * попытку).
                wait = 3 * attempt
                match = re.search(r"retry in ([\d.]+)\s*s", resp.text, re.IGNORECASE)
                if match:
                    wait = min(float(match.group(1)) + 1, 90)  # +1с запас, потолок 90с
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                last_err = RouterAIError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                if resp.status_code in (400, 401, 403):
                    break  # ключ невалиден/нет доступа к модели — повтор не поможет
                continue

            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates or not candidates[0].get("content", {}).get("parts"):
                finish_reason = (candidates[0].get("finishReason") if candidates else None) or "нет candidates"
                last_err = RouterAIError(f"Gemini вернул пустой ответ (finishReason={finish_reason})")
                break  # обычно это блокировка safety-фильтром, повтор не поможет
            text = "".join(p.get("text", "") for p in candidates[0]["content"]["parts"])
            meta = data.get("usageMetadata", {}) or {}
            usage = {
                "prompt_tokens": meta.get("promptTokenCount", 0),
                "completion_tokens": meta.get("candidatesTokenCount", 0),
                "total_tokens": meta.get("totalTokenCount", 0),
                "cost": 0.0,  # бесплатный тир — реальная стоимость всегда 0
            }
            return {"text": text, "usage": usage, "raw": data, "elapsed": elapsed}

        last_err_text = str(last_err) if last_err else ""
        if "RESOURCE_EXHAUSTED" in last_err_text or "quota" in last_err_text.lower():
            # По просьбе пользователя (2026-07-20, поймано живьём на /kb и
            # AI-анализе комплексов): вместо сырого JSON-дампа 429-ошибки —
            # понятное сообщение с реальным следующим шагом. Это ДНЕВНАЯ
            # квота бесплатного тира (RESOURCE_EXHAUSTED), не короткий
            # rate-limit — дальнейшие retry с тем же провайдером не помогут
            # до сброса лимита.
            raise RouterAIError(
                "Бесплатная дневная квота corporate Gemini (gemini_direct) на сегодня "
                "исчерпана. Подожди до сброса лимита (обычно раз в сутки) или переключи "
                "провайдера на OpenRouter (платно) в выпадающем списке на этой странице."
            )
        raise RouterAIError(f"_chat_gemini_direct() не удался после {retries} попыток: {last_err}")

    def gemini_ping(self) -> int:
        """Лёгкая проверка доступности прямого Gemini API БЕЗ траты дневной
        квоты generateContent — список моделей (metadata-эндпоинт) в неё не
        считается, в отличие от обычного chat()-вызова. Возвращает число
        моделей в каталоге (просто чтобы healthcheck мог напечатать что-то
        содержательное).

        Добавлено 2026-07-20: health_monitor.py гоняет healthcheck.py каждого
        коннектора раз в 20 минут в фоне (+ ручная кнопка "Проверить сейчас")
        — раньше это означало реальный chat()-вызов на уровне nano каждый
        раз, а nano/flash переведены на gemini_direct (см. models.yaml).
        72 фоновых вызова/сутки сами по себе съедали заметную часть и так
        небольшой бесплатной дневной квоты, из-за чего пользователь ловил
        RESOURCE_EXHAUSTED на обычных запросах (KB, AI-анализ комплексов),
        хотя сам почти не успевал попользоваться. healthcheck.py теперь
        зовёт этот метод вместо chat(), когда tier "nano" настроен на
        gemini_direct."""
        if not self.gemini_api_key:
            raise RouterAIError("GEMINI_API_KEY не задан")
        resp = self._gemini_session.get(f"{GEMINI_BASE_URL}/models", timeout=15)
        if resp.status_code >= 400:
            raise RouterAIError(f"gemini_ping() HTTP {resp.status_code}: {resp.text[:300]}")
        return len(resp.json().get("models", []))

    def embed(
        self, texts: list[str], model: str | None = None, task_label: str = "embed", retries: int = 3
    ) -> list[list[float]]:
        """
        retries=3 по умолчанию (не 2, как у chat()) — эмбеддинги вызываются
        батчами в цикле на сотни/тысячи чанков (kb_index.py), там же дороже
        падать посреди длинного прогона. Таймаут увеличен относительно
        DEFAULT_TIMEOUT — батч из EMBED_BATCH текстов обрабатывается дольше
        одного chat-запроса, поймано на реальном прогоне (ReadTimeoutError на
        60с при батче в 64 текста).
        """
        model = model or self.config.get("embedding", {}).get("model", "openai/text-embedding-3-small")
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                resp = self._session.post(
                    f"{BASE_URL}/embeddings",
                    json={"model": model, "input": texts},
                    timeout=120,
                )
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 * attempt)
                continue
            if resp.status_code == 429 and attempt < retries:
                time.sleep(2 * attempt)
                continue
            if resp.status_code >= 400:
                last_err = RouterAIError(f"embed() HTTP {resp.status_code}: {resp.text[:500]}")
                if resp.status_code in (401, 402):
                    break
                time.sleep(2 * attempt)
                continue
            break
        else:
            resp = None

        if resp is None or resp.status_code >= 400:
            raise RouterAIError(f"embed() не удался после {retries} попыток: {last_err}")

        data = resp.json()
        self._log_usage(
            task_label=task_label, tier="embedding", provider="openrouter", model=model,
            usage=data.get("usage", {}) or {}, elapsed=0.0,
        )
        return [item["embedding"] for item in data["data"]]

    # ------------------------------------------------------------------ #
    # прикладные обёртки
    # ------------------------------------------------------------------ #

    def classify(self, text: str, categories: list[str], tier: str = "nano", task_label: str = "classify") -> str:
        """Возвращает одну категорию из categories (или 'другое', если не подошла ни одна)."""
        cats = ", ".join(categories)
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты классификатор задач/заявок. Верни ровно одно слово/фразу из "
                    f"списка допустимых категорий: {cats}, другое. Никаких пояснений, "
                    "только категория."
                ),
            },
            {"role": "user", "content": text},
        ]
        result = self.chat(messages, tier=tier, task_label=task_label, max_tokens=20)
        return result["text"].strip().strip(".")

    def summarize(
        self,
        text: str,
        instructions: str = "",
        tier: str = "flash",
        task_label: str = "summarize",
        provider: str | None = None,
        retries: int = 2,
    ) -> str:
        """provider — явный override из UI ("gemini_direct"/"openrouter"),
        например выбор пользователя на экране Discord-сводок. None (по
        умолчанию) — берём provider из настроек tier в models.yaml, как
        раньше. См. chat() — именно там override реально применяется.

        retries — по умолчанию 2, как у chat(). Map-стадия суммаризации
        Discord (много nano-вызовов подряд на gemini_direct) реально ловит
        429 бесплатного тира на живых данных — вызывающий код может передать
        больше попыток, chat() теперь умеет ждать столько, сколько попросил
        сервер (см. _chat_gemini_direct)."""
        prompt = instructions or "Сделай краткую сводку по-русски, выдели ключевые факты."
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ]
        overrides = {"provider": provider} if provider else {}
        return self.chat(messages, tier=tier, task_label=task_label, retries=retries, **overrides)["text"]

    def report(
        self,
        question: str,
        items: list[dict],
        tier: str = "flash",
        task_label: str = "report",
        max_item_chars: int = 600,
        provider: str | None = None,
    ) -> str:
        """
        items: [{"ref": "SD-1025", "source": "Service Desk", "title": ..., "status": ...,
                 "url": ..., "text": "..."}]
        Модель обязана ссылаться на источники в формате [N], где N — порядковый номер
        в списке ниже. Возвращает markdown-текст с плейсхолдерами [N] — их разворачивает
        в реальные ссылки core/reports/generate.py (там есть URL, здесь модели URL не
        показываем, чтобы не тратить токены и не рисковать, что модель их исказит).

        provider — явный override из UI (см. summarize() выше, тот же смысл): None —
        provider из tier в models.yaml как раньше.
        """
        numbered = []
        for i, item in enumerate(items, start=1):
            text = (item.get("text") or "")[:max_item_chars]
            numbered.append(
                f"[{i}] источник={item.get('source')} ref={item.get('ref')} "
                f"статус={item.get('status')} заголовок={item.get('title')}\n{text}"
            )
        sources_block = "\n\n".join(numbered)

        messages = [
            {
                "role": "system",
                "content": (
                    "Ты аналитик сервис-деска. Отвечай по-русски, по существу, структурированно "
                    "(заголовки/списки уместны в этом формате). Каждый фактологический вывод "
                    "обязан ссылаться на источник в формате [N], где N — номер из списка "
                    "источников ниже. Если данных недостаточно для вывода — так и скажи, не "
                    "придумывай. Не упоминай источники, которых нет в списке.\n\n"
                    "Если вопрос прямо просит график/диаграмму/визуализацию, или числовая "
                    "разбивка (по статусам/дням/категориям и т.п.) явно просится в график — "
                    "добавь В КОНЦЕ ОТВЕТА ровно один блок ТОЧНО такого вида (не больше одного, и "
                    "только если данных для него реально достаточно). Это ОБЯЗАТЕЛЬНО должен быть "
                    "валидный JSON внутри тройных обратных кавычек с меткой именно 'chart' — НЕ "
                    "mermaid, НЕ ascii-график, НЕ markdown-таблица, никакой другой формат диаграммы "
                    "не поддерживается и не будет показан пользователю:\n"
                    "```chart\n"
                    '{"type": "bar", "title": "...", "labels": ["...", "..."], '
                    '"datasets": [{"label": "...", "data": [1, 2, 3]}]}\n'
                    "```\n"
                    "Пример для распределения заявок по статусам (5 новых, 3 в работе, 2 закрыто):\n"
                    "```chart\n"
                    '{"type": "pie", "title": "Заявки по статусам", '
                    '"labels": ["Новая", "В работе", "Закрыта"], '
                    '"datasets": [{"label": "Заявки", "data": [5, 3, 2]}]}\n'
                    "```\n"
                    "type — 'bar', 'line' или 'pie'. Если график не нужен или данных для него "
                    "недостаточно — не добавляй этот блок вообще, но никогда не заменяй его mermaid "
                    "или другим форматом."
                ),
            },
            {
                "role": "user",
                "content": f"Вопрос/задача: {question}\n\nИсточники:\n{sources_block}",
            },
        ]
        overrides = {"provider": provider} if provider else {}
        return self.chat(
            messages, tier=tier, task_label=task_label,
            max_tokens=self._tier_config(tier).get("max_tokens", 2000), **overrides,
        )["text"]

    # ------------------------------------------------------------------ #
    # служебное
    # ------------------------------------------------------------------ #

    def key_info(self) -> dict:
        """GET /api/v1/key — лимит/остаток кредитов, бесплатно, токены не тратит."""
        resp = self._session.get(f"{BASE_URL}/key", timeout=15)
        if resp.status_code >= 400:
            raise RouterAIError(f"key_info() HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("data", {})

    def _log_usage(
        self, task_label: str, tier: str, provider: str, model: str, usage: dict, elapsed: float
    ) -> None:
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not USAGE_LOG_PATH.exists()
        with open(USAGE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(USAGE_LOG_FIELDS)
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    task_label,
                    tier,
                    provider,
                    model,
                    usage.get("prompt_tokens", ""),
                    usage.get("completion_tokens", ""),
                    usage.get("total_tokens", ""),
                    usage.get("cost", ""),
                    f"{elapsed:.2f}",
                ]
            )


if __name__ == "__main__":
    # Быстрый ручной прогон: python client.py "вопрос"
    import sys

    client = RouterAIClient()
    q = sys.argv[1] if len(sys.argv) > 1 else "Скажи 'привет' одним словом"
    out = client.chat([{"role": "user", "content": q}], tier="nano", task_label="manual_test")
    print(out["text"])
    print("usage:", out["usage"])
