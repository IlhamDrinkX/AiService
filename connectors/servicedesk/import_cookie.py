"""
Альтернатива login.py: вместо запуска отдельного браузера (который у части
пользователей не может достучаться до sd.drinkx.tech из свежего
Chrome-профиля — похоже, доступ завязан на VPN-расширение/политику/
сертификат конкретного профиля), просто забираем cookie из уже рабочей,
залогиненной вкладки руками.

Как получить строку cookie:
  1. Открой sd.drinkx.tech в браузере, где он уже нормально работает
     (залогинен, видишь список заявок).
  2. F12 -> вкладка Network -> обнови страницу (F5).
  3. Кликни на любой запрос к sd.drinkx.tech (например tickets или
     prototype/tickets).
  4. Справа -> Headers -> Request Headers -> найди строку "cookie:" ->
     скопируй ЗНАЧЕНИЕ целиком (это одна длинная строка вида
     "name1=value1; name2=value2; ...").

Запуск:
  python import_cookie.py
  (скрипт попросит вставить строку cookie и сохранит её в STATE_PATH)
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

STATE_PATH = os.environ.get("SERVICEDESK_STATE_PATH", "./servicedesk_state.json")


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
    print("Вставь строку из DevTools -> Network -> запрос к sd.drinkx.tech -> ")
    print("Headers -> Request Headers -> 'cookie:' (Enter, затем Ctrl+D / Ctrl+Z чтобы закончить):")
    raw = input("> ").strip()

    cookies = parse_cookie_header(raw)
    if not cookies:
        print("Не нашёл ни одной пары name=value — проверь, что скопировал именно значение заголовка cookie.")
        return

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)

    names = ", ".join(c["name"] for c in cookies)
    print(f"Сохранено {len(cookies)} cookie в {STATE_PATH}: {names}")
    print("Можно запускать sync.py.")


if __name__ == "__main__":
    main()
