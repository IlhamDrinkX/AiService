"""
Google Drive sync — read-only, scans My Drive + every Shared Drive the
account has access to.

MVP approach: full re-scan of file metadata every run (cheap, one paginated
API call per drive), but only re-downloads+re-extracts text for files whose
modifiedTime changed since last run. Good enough at laptop/company scale;
can be swapped for the Changes API later if the file count gets large enough
that a full metadata scan becomes slow.
"""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from auth import get_services
from extract import EXTRACTABLE_MIMES, extract_text
from retry import retry_call
from storage import DriveStorage

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "./data/drive.db")
# 0 = без ограничения (по умолчанию) — документы вычитываются целиком,
# сколько бы в них ни было страниц. Раньше дефолт был 50000 символов
# (~15-20 страниц), из-за чего длинные документы молча обрезались —
# TEXT_MAX_CHARS теперь opt-in предохранитель, а не потолок по умолчанию.
TEXT_MAX_CHARS = int(os.environ.get("TEXT_MAX_CHARS", "0"))
MAX_FILE_SIZE_MB = float(os.environ.get("MAX_FILE_SIZE_MB", "25"))

FIELDS = "nextPageToken, files(id,name,mimeType,modifiedTime,parents,webViewLink,size)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("drive_connector")


def list_all_drives(service) -> list[dict]:
    drives = [{"id": None, "name": "My Drive"}]
    page_token = None
    while True:
        resp = retry_call(
            lambda: service.drives()
            .list(pageSize=100, pageToken=page_token, fields="nextPageToken, drives(id,name)")
            .execute(),
            what="drives.list",
        )
        for d in resp.get("drives", []):
            drives.append({"id": d["id"], "name": d["name"]})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return drives


def list_files(service, drive_id: str | None) -> list[dict]:
    files = []
    page_token = None
    while True:
        params = dict(q="trashed = false", fields=FIELDS, pageSize=1000, pageToken=page_token)
        if drive_id:
            params.update(
                corpora="drive",
                driveId=drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
        else:
            params.update(corpora="user")
        resp = retry_call(lambda: service.files().list(**params).execute(), what="files.list")
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def sync():
    services = get_services()
    service = services["drive"]  # list_all_drives/list_files only ever touch Drive's own API
    storage = DriveStorage(DB_PATH)

    drives = list_all_drives(service)
    log.info("Found %d drive(s) (including My Drive)", len(drives))

    total_files = 0
    newly_extracted = 0

    for d in drives:
        try:
            files = list_files(service, d["id"])
        except Exception as e:
            log.error("Giving up on drive '%s' after retries: %s — skipping it this run", d["name"], e)
            continue
        log.info("Drive '%s': %d files", d["name"], len(files))

        for i, f in enumerate(files, start=1):
            total_files += 1
            size_bytes = int(f.get("size", 0) or 0)
            is_extractable = f["mimeType"] in EXTRACTABLE_MIMES
            changed = storage.get_modified_time(f["id"]) != f.get("modifiedTime")

            # Heartbeat: one line per file, so a long run never looks frozen —
            # even files we skip instantly still advance the counter.
            log.info(
                "[%d/%d] %s: '%s' (%.1f MB)%s",
                i,
                len(files),
                d["name"],
                f["name"],
                size_bytes / 1024 / 1024,
                " — extracting..." if is_extractable and changed else "",
            )

            text_content = None
            if is_extractable and changed:
                if size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
                    log.warning("Skipping content of %s: %.1f MB > limit (MAX_FILE_SIZE_MB in .env)", f["name"], size_bytes / 1024 / 1024)
                else:
                    text_content = extract_text(services, f, TEXT_MAX_CHARS)
                    if text_content:
                        newly_extracted += 1
                        log.info("  -> extracted %d chars", len(text_content))
                    else:
                        log.info("  -> no text extracted (unsupported/empty/failed)")
            elif is_extractable and not changed:
                text_content = storage.get_text(f["id"])  # unchanged, keep what we had

            storage.save_file(
                {
                    "file_id": f["id"],
                    "drive_id": d["id"],
                    "drive_name": d["name"],
                    "name": f["name"],
                    "mime_type": f["mimeType"],
                    "modified_time": f.get("modifiedTime"),
                    "web_view_link": f.get("webViewLink"),
                    "parents": f.get("parents", []),
                    "size_bytes": size_bytes,
                    "text_content": text_content,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    log.info(
        "Done. Files seen: %d, newly extracted: %d, total stored: %d, with text: %d",
        total_files,
        newly_extracted,
        storage.count(),
        storage.count_with_text(),
    )


if __name__ == "__main__":
    sync()
