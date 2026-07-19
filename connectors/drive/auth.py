"""
OAuth2 flow for Google Drive — same pattern as connectors/gmail/auth.py, but
its own token/scope.

Three scopes, three services, one OAuth consent:
- drive.readonly — My Drive + any Shared Drives the account is already a
  member of (no domain admin needed); also used to download raw bytes for
  PDF/docx/xlsx/pptx/rtf/txt/csv.
- spreadsheets.readonly — needed to read EVERY sheet/tab of a Google Sheet.
  Drive's own files.export(mimeType=text/csv) only ever returns the first
  sheet (a CSV has no concept of multiple tabs) — there is no way to get
  the rest of the tabs through the Drive API alone, the Sheets API is the
  only way to enumerate and read all of them.
- presentations.readonly — needed to read speaker notes on Google Slides.
  Drive's files.export(mimeType=text/plain) only returns the visible
  on-slide text, never the notes — same story as Sheets, a different API
  is the only way to get at that data.

2026-07-19: added the latter two scopes to collect Sheets/Slides data
completely instead of the subset Drive's plain export API exposes. Anyone
who already has a `data/token.json` from before this change needs to
re-consent once — see `_needs_reauth()` below, which detects a token that's
missing a scope and forces a fresh consent screen automatically instead of
failing with a cryptic 403 mid-sync.
"""

import os

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Default socket timeout is too short for exporting/downloading large files
# (Google Docs export, big PDFs) over an ordinary office connection.
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "60"))

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
]

CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "./data/token.json")


def _needs_reauth(creds) -> bool:
    """True if a previously-saved token is missing a scope we now require
    (e.g. an older token.json from before spreadsheets/presentations were
    added) — an expired-but-refreshable token would otherwise silently keep
    the old, narrower scope forever since refreshing doesn't add scopes."""
    if creds is None:
        return True
    granted = set(creds.scopes or [])
    return not set(SCOPES).issubset(granted)


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if _needs_reauth(creds):
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
    elif not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    return creds


def get_services() -> dict:
    """Returns {"drive": ..., "sheets": ..., "slides": ...} — one shared
    AuthorizedHttp/credentials, three API clients."""
    creds = get_credentials()
    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS))
    return {
        "drive": build("drive", "v3", http=authorized_http),
        "sheets": build("sheets", "v4", http=authorized_http),
        "slides": build("slides", "v1", http=authorized_http),
    }
