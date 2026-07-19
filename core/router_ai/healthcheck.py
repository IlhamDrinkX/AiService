"""
Лёгкая проверка router_ai_client. Два независимых блока:
- OpenRouter: бесплатный запрос баланса ключа (GET /api/v1/key — токены не
  тратит).
- Уровень "nano" (nano/flash сейчас на gemini_direct, см. models.yaml):
  если провайдер openrouter — один минимальный chat-вызов, чтобы
  подтвердить, что раунд-трип реально работает, а не только что ключ
  валиден; если провайдер gemini_direct — `client.gemini_ping()` вместо
  chat(), см. его docstring в client.py про экономию дневной квоты.
  Это правило было добавлено 2026-07-20 живьём: фоновая проверка каждые
  20 минут (health_monitor.py) реальным chat()-вызовом сама по себе
  заметно выедала бесплатную дневную квоту gemini_direct, из-за чего
  обычные запросы пользователя (KB, AI-анализ комплексов) ловили
  RESOURCE_EXHAUSTED раньше, чем он успевал ими воспользоваться.

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1) — тот же
формат, что у healthcheck.py остальных коннекторов (см. check_connections.py).
"""

import sys

from client import RouterAIClient, RouterAIError


def main():
    try:
        client = RouterAIClient()
    except RouterAIError as e:
        print(f"FAIL {e}")
        sys.exit(1)

    parts = []
    ok = True

    if client.api_key:
        try:
            info = client.key_info()
            parts.append(
                f"OpenRouter: лимит={info.get('limit')}, остаток={info.get('limit_remaining')}, "
                f"потрачено всего={info.get('usage')}"
            )
        except RouterAIError as e:
            ok = False
            parts.append(f"OpenRouter key_info FAIL: {e}")

    nano_provider = client._tier_config("nano").get("provider", "openrouter")

    if nano_provider == "gemini_direct":
        try:
            n_models = client.gemini_ping()
            parts.append(
                f"corporate Gemini: ключ валиден, моделей в каталоге={n_models} "
                "(chat-вызов пропущен, чтобы не тратить бесплатную дневную квоту)"
            )
        except RouterAIError as e:
            ok = False
            parts.append(f"corporate Gemini FAIL: {e}")
    else:
        try:
            result = client.chat(
                [{"role": "user", "content": "Ответь одним словом: OK"}],
                tier="nano",
                task_label="healthcheck",
                max_tokens=10,
            )
            tokens = result["usage"].get("total_tokens", "?")
            cost = result["usage"].get("cost", "?")
            parts.append(f"тестовый chat-вызов ok: '{result['text'].strip()[:30]}' ({tokens} токенов, ${cost})")
        except RouterAIError as e:
            ok = False
            parts.append(f"тестовый chat-вызов FAIL: {e}")

    msg = "; ".join(parts) if parts else "нечего проверять — нет ни OPENROUTER_API_KEY, ни настроенного gemini_direct"
    if ok and parts:
        print(f"OK {msg}")
        sys.exit(0)
    print(f"FAIL {msg}")
    sys.exit(1)


if __name__ == "__main__":
    main()
