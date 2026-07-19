"""
ВНИМАНИЕ: на машине, где это разрабатывалось, этот способ не работает —
sd.drinkx.tech недоступен из свежего Chrome-профиля (похоже, доступ завязан
на VPN-расширение/корп-политику/сертификат конкретного профиля браузера).
Основной способ получить сессию — import_cookie.py (копируешь cookie из
DevTools своей рабочей вкладки руками). Этот файл оставлен на случай, если
на другой машине без такого ограничения интерактивный логин через Playwright
всё же сработает.

Одноразовый интерактивный логин в Service Desk (sd.drinkx.tech) через
настоящий Chrome (Playwright) — SSO домена drinkx.tech делает обычный вход
по API невозможным без прохождения формы Google-логина руками.

Что делает:
  1. Открывает окно браузера на sd.drinkx.tech.
  2. Ждёт, пока ты руками пройдёшь Google SSO и попадёшь на страницу списка
     заявок (или пока API /api/prototype/tickets не начнёт отвечать 200 —
     это и есть подтверждение, что сессия установлена).
  3. Сохраняет cookies (+ localStorage) в STATE_PATH — client.py потом
     переиспользует эти cookies через curl_cffi, без браузера.

Запускать заново, когда client.py/sync.py начнут падать с ServiceDeskAuthError
(сессия истекла или cookie отозван).
"""

import os
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

BASE_URL = os.environ.get("SERVICEDESK_BASE_URL", "https://sd.drinkx.tech")
STATE_PATH = os.environ.get("SERVICEDESK_STATE_PATH", "./servicedesk_state.json")
TICKETS_ENDPOINT = f"{BASE_URL}/api/prototype/tickets"
POLL_TIMEOUT_MS = 5 * 60 * 1000  # 5 минут на ручной вход


def main():
    with sync_playwright() as p:
        # channel="chrome" — запускаем настоящий установленный Chrome, а не
        # встроенный в Playwright Chromium. Плюс убираем флаг
        # navigator.webdriver и связанные с ним признаки автоматизации:
        # похоже, что sd.drinkx.tech (или что-то перед ним) детектит
        # управляемый браузер и вместо явной ошибки просто "подвешивает"
        # соединение — тот же паттерн стопора, что мы видели у трекера.
        try:
            browser = p.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            print(
                "Не нашёл установленный Chrome (channel='chrome') — "
                "использую встроенный Chromium. Если проблема из-за детекта "
                "автоматизации, могут понадобиться `python -m playwright install chrome`.",
                file=sys.stderr,
            )
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )

        context = browser.new_context()
        # CDP всё равно выставляет navigator.webdriver=true при коннекте —
        # флага --disable-blink-features достаточно не всегда, поэтому
        # дополнительно перебиваем его через init-script до загрузки страницы.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        # Видимость происходящего: если страница снова зависнет, будет видно,
        # какой конкретно запрос стопорится / какая ошибка в консоли страницы,
        # вместо голого "белого экрана".
        page.on(
            "requestfailed",
            lambda req: print(f"  [сеть] запрос не прошёл: {req.url} — {req.failure}", file=sys.stderr),
        )
        page.on(
            "console",
            lambda msg: print(f"  [консоль страницы] {msg.text}", file=sys.stderr) if msg.type == "error" else None,
        )

        print(f"Открываю {BASE_URL} — пройди вход через Google SSO (аккаунт @drinkx.tech).")
        # domcontentloaded вместо дефолтного load: сайт грузится долго, но нам
        # достаточно, чтобы страница начала отвечать на клики, а не полностью
        # прогрузилась (иначе page.goto сам падает по таймауту).
        try:
            page.goto(BASE_URL, timeout=90_000, wait_until="domcontentloaded")
        except Exception as e:
            print(
                f"Не удалось загрузить {BASE_URL} за 90 секунд: {e}\n"
                "Смотри строки '[сеть]'/'[консоль страницы]' выше — там видно, что именно "
                "зависло. Если ничего не выводится, вероятно, соединение стопорится на "
                "уровне TLS/WAF (как было с трекером), а не в самой странице.",
                file=sys.stderr,
            )
            browser.close()
            sys.exit(1)

        print("Жду, когда сессия установится (проверяю /api/prototype/tickets)...")
        try:
            page.wait_for_function(
                f"""
                async () => {{
                    try {{
                        const r = await fetch({TICKETS_ENDPOINT!r}, {{credentials: 'include'}});
                        if (r.status !== 200) return false;
                        // Важно: 200 сам по себе ничего не доказывает — у SPA часто
                        // настроен catch-all, отдающий 200 + index.html на любой путь,
                        // включая /api/*, пока бэкенд/сессия ещё не готовы. Поэтому
                        // дополнительно проверяем, что ответ реально JSON-массив тикетов,
                        // а не HTML-заглушка.
                        const ct = r.headers.get('content-type') || '';
                        if (!ct.includes('application/json')) return false;
                        const data = await r.json();
                        return Array.isArray(data);
                    }} catch (e) {{
                        return false;
                    }}
                }}
                """,
                timeout=POLL_TIMEOUT_MS,
                polling=2000,
            )
        except Exception:
            print(
                "Не дождался успешного ответа API за 5 минут. "
                "Если ты уже вошёл — просто запусти скрипт заново, cookie сохранится "
                "и следующая проверка пройдёт быстрее.",
                file=sys.stderr,
            )
            browser.close()
            sys.exit(1)

        context.storage_state(path=STATE_PATH)
        print(f"Готово. Сессия сохранена в {STATE_PATH}.")
        browser.close()


if __name__ == "__main__":
    main()
