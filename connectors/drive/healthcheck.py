"""
Лёгкая проверка Google Drive-коннектора: тихо обновляет токен, если истёк
(без интерактивного входа), и делает один минимальный вызов API
(about().get — не листит файлы).

Печатает "OK <детали>" (exit 0) или "FAIL <причина>" (exit 1).
"""

import os
import sys

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "./data/token.json")


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

    try:
        service = build("drive", "v3", credentials=creds)
        about = service.about().get(fields="user").execute()
    except Exception as e:
        print(f"FAIL API не отвечает: {e}")
        sys.exit(1)

    print(f"OK {about.get('user', {}).get('emailAddress', '?')}")
    sys.exit(0)


if __name__ == "__main__":
    main()
