"""
Лёгкая проверка коннектора sheets_export: тихо обновляет токен, если истёк
(без интерактивного входа), и, если таблица уже была создана раньше,
делает один read-only вызов (spreadsheets.get, только метаданные — без
values, ничего не тратит и не перезаписывает).

Если таблицы ещё нет (первый запуск sync.py ещё не выполнялся) — просто
подтверждает, что токен валиден, без создания таблицы (создание — не
"лёгкая проверка", а реальное действие, ему тут не место).

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1).
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "./data/token.json")
STATE_PATH = ROOT / "sheet_state.json"


def main():
    if not os.path.exists(TOKEN_PATH):
        print(f"FAIL нет токена {TOKEN_PATH} — нужен вход (python sync.py руками)")
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"FAIL не смог обновить токен: {e}")
                sys.exit(1)
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            print("FAIL токен невалиден и не обновляется — нужен повторный вход")
            sys.exit(1)

    sheet_id = None
    if STATE_PATH.exists():
        try:
            sheet_id = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("spreadsheet_id")
        except (json.JSONDecodeError, OSError):
            pass

    try:
        service = build("sheets", "v4", credentials=creds)
        if sheet_id:
            meta = service.spreadsheets().get(
                spreadsheetId=sheet_id, fields="properties.title"
            ).execute()
            print(f"OK токен валиден, таблица доступна: {meta.get('properties', {}).get('title')}")
        else:
            print("OK токен валиден, таблица ещё не создана (первый python sync.py её создаст)")
    except Exception as e:
        print(f"FAIL API не отвечает: {e}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
