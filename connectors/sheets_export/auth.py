"""
OAuth2 flow для записи в Google Sheets — тот же паттерн, что и
connectors/drive/auth.py, но свой scope и свой token.json (не переиспользуем
токен Drive: у него только *.readonly, а этому коннектору нужна запись).

Один scope, spreadsheets (полный, не .readonly) — этого достаточно и для
создания новой таблицы (spreadsheets.create), и для чтения/перезаписи
данных в ней. Права "кто видит таблицу" (Поделиться) через этот scope не
выставить — для этого нужен отдельный Drive-scope (drive.file или полный
drive), который мы сознательно не запрашиваем: расшаривание — разовое
ручное действие после первого запуска (см. README), а не то, что скрипту
нужно делать программно на каждый прогон.
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "./data/token.json")


def _needs_reauth(creds) -> bool:
    if creds is None:
        return True
    granted = set(creds.scopes or [])
    return not set(SCOPES).issubset(granted)


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    # timeout_seconds — по умолчанию у google-auth-oauthlib он короткий, и
    # реального человека, проходящего вход/2FA в браузере, легко не
    # уложить в него (поймано вживую: WSGITimeoutError, сервер закрылся
    # раньше, чем браузер успел вернуться с кодом). 5 минут с запасом.
    if _needs_reauth(creds):
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0, timeout_seconds=300)
    elif not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0, timeout_seconds=300)

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    return creds


def get_services() -> dict:
    creds = get_credentials()
    return {"sheets": build("sheets", "v4", credentials=creds)}
