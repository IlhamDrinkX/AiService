"""
DrinkX Tracker sync — тянет доски и задачи через тот же cookie-based
TrackerClient, что используется и для записи (см. client.py). Раньше это
был отдельный read-only Bearer-token API — теперь один клиент на всё.
"""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from client import TrackerAuthError, TrackerClient
from storage import TrackerStorage

load_dotenv()

BASE_URL = os.environ.get("TRACKER_BASE_URL", "https://tracker.drinkx.tech")
STATE_PATH = os.environ.get("TRACKER_STATE_PATH", "./tracker_state.json")
DB_PATH = os.environ.get("DB_PATH", "./data/tracker.db")
# Пусто = все доски, которые вернёт /api/task-types. Можно ограничить списком кодов через запятую.
BOARD_CODES = {c.strip() for c in os.environ.get("BOARD_CODES", "").split(",") if c.strip()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker_connector")


def _resolve_status_names(boards: list[dict]) -> dict[str, str]:
    mapping = {}
    for board in boards:
        for status in board.get("statuses", []):
            mapping[status["id"]] = status["name"]
    return mapping


def _to_record(task: dict, status_names: dict[str, str], fetched_at: str) -> dict:
    assignee = task.get("assignee") or {}
    reporter = task.get("reporter") or {}
    parent = task.get("parent") or {}
    return {
        "task_id": task["id"],
        "code": task.get("code"),
        "task_type_code": task.get("taskTypeCode"),
        "sequence_number": task.get("sequenceNumber"),
        "title": task.get("title"),
        "description": task.get("description"),
        "status_id": task.get("statusId"),
        "status_name": status_names.get(task.get("statusId"), "?"),
        "status_changed_at": task.get("statusChangedAt"),
        "is_urgent": task.get("isUrgent", False),
        "assignee_name": assignee.get("name"),
        "assignee_email": assignee.get("email"),
        "reporter_name": reporter.get("name"),
        "reporter_email": reporter.get("email"),
        "tags": task.get("tags", []),
        "subtask_count": task.get("subtaskCount", 0),
        "parent_code": parent.get("code"),
        "last_activity_at": task.get("lastActivityAt"),
        "created_at": task.get("createdAt"),
        "updated_at": task.get("updatedAt"),
        "fetched_at": fetched_at,
    }


def sync():
    client = TrackerClient(BASE_URL, state_path=STATE_PATH)
    storage = TrackerStorage(DB_PATH)
    fetched_at = datetime.now(timezone.utc).isoformat()

    try:
        boards = client.get_task_types()
    except TrackerAuthError as e:
        log.error(str(e))
        return

    log.info("Found %d board(s): %s", len(boards), ", ".join(b["code"] for b in boards))
    for board in boards:
        board["fetched_at"] = fetched_at
        storage.save_board(board)

    status_names = _resolve_status_names(boards)

    codes = BOARD_CODES or {b["code"] for b in boards}
    total_tasks = 0
    for code in codes:
        try:
            tasks = client.get_tasks(code)
        except TrackerAuthError as e:
            log.error(str(e))
            return
        log.info("Board '%s': %d tasks", code, len(tasks))
        for task in tasks:
            storage.save_task(_to_record(task, status_names, fetched_at))
            total_tasks += 1

    log.info(
        "Done. Boards: %d, tasks fetched: %d, total stored: %d",
        storage.count_boards(),
        total_tasks,
        storage.count_tasks(),
    )


if __name__ == "__main__":
    sync()
