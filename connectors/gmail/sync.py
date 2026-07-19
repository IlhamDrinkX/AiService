"""
Gmail sync — read-only.

Run with no stored historyId -> full sync of the whole mailbox (all labels,
spam/trash excluded by default). On later runs, uses the History API to pull
only what changed since the last run instead of re-scanning everything.

Intended to be run periodically (cron / Task Scheduler), not as a long-lived
process — Gmail's real push notifications require a Pub/Sub + domain setup
that's overkill for a single mailbox on a laptop.
"""

import base64
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from auth import get_gmail_service
from storage import GmailStorage

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "./data/gmail.db")
INCLUDE_SPAM_TRASH = os.environ.get("INCLUDE_SPAM_TRASH", "false").lower() == "true"
BODY_MAX_CHARS = int(os.environ.get("BODY_MAX_CHARS", "20000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gmail_connector")


def _header(headers: list[dict], name: str) -> str | None:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _extract_body(payload: dict) -> str:
    """Walk MIME parts, prefer text/plain, fall back to text/html (raw, untagged)."""

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            result = walk(sub)
            if result:
                return result
        if mime == "text/html" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return None

    return (walk(payload) or "")[:BODY_MAX_CHARS]


def _extract_attachments(payload: dict) -> list[dict]:
    found = []

    def walk(part):
        filename = part.get("filename")
        body = part.get("body", {})
        if filename and body.get("attachmentId"):
            found.append(
                {
                    "filename": filename,
                    "attachment_id": body["attachmentId"],
                    "mime_type": part.get("mimeType"),
                }
            )
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return found


def _to_record(full_message: dict) -> dict:
    payload = full_message.get("payload", {})
    headers = payload.get("headers", [])
    internal_ts = int(full_message.get("internalDate", "0")) / 1000
    return {
        "message_id": full_message["id"],
        "thread_id": full_message.get("threadId"),
        "sender": _header(headers, "From"),
        "to_recipients": _header(headers, "To"),
        "cc_recipients": _header(headers, "Cc"),
        "subject": _header(headers, "Subject"),
        "date": datetime.fromtimestamp(internal_ts, tz=timezone.utc).isoformat(),
        "snippet": full_message.get("snippet"),
        "body_text": _extract_body(payload),
        "labels": full_message.get("labelIds", []),
        "attachments": _extract_attachments(payload),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_and_store(service, storage: GmailStorage, message_id: str) -> None:
    try:
        full = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except HttpError as e:
        log.warning("Failed to fetch message %s: %s", message_id, e)
        return
    storage.save_message(_to_record(full))


def full_sync(service, storage: GmailStorage) -> None:
    log.info("No stored historyId — running full mailbox sync")
    page_token = None
    total = 0
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", pageToken=page_token, includeSpamTrash=INCLUDE_SPAM_TRASH, maxResults=500)
            .execute()
        )
        for m in resp.get("messages", []):
            fetch_and_store(service, storage, m["id"])
            total += 1
        page_token = resp.get("nextPageToken")
        log.info("Synced %d messages so far...", total)
        if not page_token:
            break

    profile = service.users().getProfile(userId="me").execute()
    storage.set_history_id(str(profile["historyId"]))
    log.info("Full sync done. Total: %d. historyId=%s", total, profile["historyId"])


def incremental_sync(service, storage: GmailStorage, start_history_id: str) -> None:
    log.info("Incremental sync from historyId=%s", start_history_id)
    page_token = None
    seen = set()
    latest_history_id = start_history_id
    try:
        while True:
            resp = (
                service.users()
                .history()
                .list(userId="me", startHistoryId=start_history_id, pageToken=page_token)
                .execute()
            )
            for record in resp.get("history", []):
                latest_history_id = record.get("id", latest_history_id)
                for added in record.get("messagesAdded", []):
                    mid = added["message"]["id"]
                    if mid not in seen:
                        seen.add(mid)
                        fetch_and_store(service, storage, mid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        if getattr(e, "status_code", None) == 404 or "404" in str(e):
            log.warning("historyId too old (Gmail expires history after ~1 week). Falling back to full sync.")
            full_sync(service, storage)
            return
        raise

    storage.set_history_id(str(latest_history_id))
    log.info("Incremental sync done. %d new messages. historyId=%s", len(seen), latest_history_id)


def main():
    service = get_gmail_service()
    storage = GmailStorage(DB_PATH)

    history_id = storage.get_history_id()
    if history_id is None:
        full_sync(service, storage)
    else:
        incremental_sync(service, storage, history_id)

    log.info("Total messages stored: %d", storage.count())


if __name__ == "__main__":
    main()
