"""
Импорт cookie-сессии трекера (аналог connectors/servicedesk/import_cookie.py).

Старый способ (Bearer-токен со страницы "Интеграции", scope tasks:read)
остаётся рабочим для чтения, но не даёт записи и действует не от имени
конкретного человека. Эта cookie-сессия — то же самое, чем пользуется сама
SPA после Google SSO (домен drinkx.tech): один httpOnly cookie `sid`,
никаких дополнительных токенов. Через неё доступны и запись, и уведомления,
и действия "от лица" залогиненного пользователя.

Как получить строку cookie:
  1. Открой tracker.drinkx.tech в браузере, где ты уже залогинен (видишь
     доску с задачами).
  2. F12 -> вкладка Network -> обнови страницу (F5).
  3. Кликни на любой запрос к tracker.drinkx.tech (например tasks или me).
  4. Справа -> Headers -> Request Headers -> найди строку "cookie:" ->
     скопируй ЗНАЧЕНИЕ целиком (это одна строка вида "sid=...; other=...").
     Можно скопировать и весь заголовок целиком — лишние пары name=value
     игнорируются, важен только sid.

Запуск:
  python import_cookie.py
  (скрипт попросит вставить строку cookie и сохранит её в STATE_PATH)
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

STATE_PATH = os.environ.get("TRACKER_STATE_PATH", "./tracker_state.json")


def parse_cookie_header(raw: str) -> list[dict]:
    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def main():
    print("Вставь строку из DevTools -> Network -> запрос к tracker.drinkx.tech -> ")
    print("Headers -> Request Headers -> 'cookie:' (нужен как минимум sid=...):")
    raw = input("> ").strip()

    cookies = parse_cookie_header(raw)
    if not cookies:
        print("Не нашёл ни одной пары name=value — проверь, что скопировал именно значение заголовка cookie.")
        return
    if not any(c["name"] == "sid" for c in cookies):
        print("Внимание: среди скопированного нет cookie 'sid' — вероятно, это не та строка.")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)

    names = ", ".join(c["name"] for c in cookies)
    print(f"Сохранено {len(cookies)} cookie в {STATE_PATH}: {names}")
    print("Можно запускать sync.py / healthcheck.py.")


if __name__ == "__main__":
    main()
