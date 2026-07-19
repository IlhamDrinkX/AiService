"""
Лёгкая проверка Discord-коннектора — без подключения к gateway (это делает
bot.py). Два REST-запроса с текущим токеном:
  1. GET /users/@me       -> токен валиден?
  2. GET /users/@me/guilds -> бот реально состоит хоть в одном сервере?

Второй запрос — прямой ответ на "бот добавлен на сервер, но не уверен, что
настроил правильно": если identify проходит, а серверов 0 — значит бот
токеном рабочий, но никуда не добавлен (или добавление не подтверждено).

Важно про заголовок User-Agent: без него Cloudflare перед discord.com иногда
отдаёт 403 (error code: 1010) — это не про права/интенты бота, а про то, что
голый запрос похож на скрипт. discord.py (в bot.py) сам подставляет
осмысленный User-Agent, поэтому там этой проблемы нет — здесь подставляем
такой же по духу, по рекомендованному Discord формату.

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1) — этот формат
разбирает ../../check_connections.py.

Без внешних зависимостей (urllib из stdlib), чтобы работать даже без
отдельного venv для этого коннектора.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/discord/discord-api-docs, 1.0) drinkx-servicedesk-healthcheck"


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def _request(path: str, token: str):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    env = load_env(Path(".env"))
    token = env.get("DISCORD_BOT_TOKEN")
    if not token:
        print("FAIL DISCORD_BOT_TOKEN не задан в .env")
        sys.exit(1)

    try:
        me = _request("/users/@me", token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"FAIL HTTP {e.code} на /users/@me: {body[:200]}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"FAIL сеть недоступна: {e}")
        sys.exit(1)

    try:
        guilds = _request("/users/@me/guilds", token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"FAIL токен рабочий (бот {me.get('username', '?')}), но /users/@me/guilds вернул HTTP {e.code}: {body[:200]}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"FAIL токен рабочий, но список серверов не запросить: {e}")
        sys.exit(1)

    if not guilds:
        print(
            f"FAIL бот {me.get('username', '?')} авторизован, но не состоит НИ В ОДНОМ сервере — "
            "проверь, что приглашение (OAuth2 URL с scope=bot) было открыто и подтверждено админом сервера"
        )
        sys.exit(1)

    names = ", ".join(g.get("name", "?") for g in guilds[:5])
    print(f"OK бот {me.get('username', '?')}, серверов: {len(guilds)} ({names})")
    sys.exit(0)


if __name__ == "__main__":
    main()
