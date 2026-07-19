"""
Подсветка упоминаний monitoring/service team в Discord-сообщениях —
без LLM, чистое сопоставление (см. functional_plan_ui.md §3.1). Конфиг —
monitoring_config.yaml.
"""

import re
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "monitoring_config.yaml"
_config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))


def _compile(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(patterns), re.IGNORECASE) if patterns else None


_MON_TEXT = _compile(_config.get("monitoring", {}).get("text_patterns", []))
_SVC_TEXT = _compile(_config.get("service", {}).get("text_patterns", []))
_MON_ROLES = set(_config.get("monitoring", {}).get("role_ids", []))
_SVC_ROLES = set(_config.get("service", {}).get("role_ids", []))


def classify(content: str) -> str | None:
    """Возвращает 'mention-monitoring' / 'mention-service' / None."""
    if not content:
        return None
    role_mentions = set(re.findall(r"<@&(\d+)>", content))
    if role_mentions & _MON_ROLES or (_MON_TEXT and _MON_TEXT.search(content)):
        return "mention-monitoring"
    if role_mentions & _SVC_ROLES or (_SVC_TEXT and _SVC_TEXT.search(content)):
        return "mention-service"
    return None
