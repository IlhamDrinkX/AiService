"""
Small retry helper for transient network errors talking to the Drive API.
Corporate wifi / large file exports occasionally time out — that shouldn't
kill an hours-long sync of thousands of files, it should just back off and
try again a few times.
"""

import http.client
import logging
import socket
import ssl
import time

from googleapiclient.errors import HttpError

log = logging.getLogger("drive_connector.retry")

TRANSIENT_EXC = (
    TimeoutError,
    socket.timeout,
    ssl.SSLError,
    ConnectionError,
    http.client.IncompleteRead,
    http.client.HTTPException,
)


def retry_call(fn, *, retries: int = 5, base_delay: float = 5.0, what: str = "request"):
    """Call fn() with exponential backoff on transient network errors / 5xx."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except TRANSIENT_EXC as e:
            last_exc = e
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if not (status and int(status) >= 500):
                raise
            last_exc = e

        delay = base_delay * attempt
        log.warning("%s failed (attempt %d/%d): %s — retrying in %.0fs", what, attempt, retries, last_exc, delay)
        time.sleep(delay)

    log.error("%s failed after %d attempts, giving up", what, retries)
    raise last_exc
