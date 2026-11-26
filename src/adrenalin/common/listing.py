"""Cursor-paginated listings on a WorkQueue: one queue item per page, and loop detection across reruns."""
from __future__ import annotations

import json
import logging
import threading

from adrenalin.common.pipeline import WorkQueue

log = logging.getLogger(__name__)


class PaginationLoop(ValueError):
    pass


def _seen_cursors(raw: str | None) -> dict[str, str]:
    """The `seen_cursors` meta: {cursor: page key that produced it}. Older runs stored a list of cursors
    (producer unknown, so any repeat of those is still a loop)."""
    data = json.loads(raw or "{}")
    return {c: "" for c in data} if isinstance(data, list) else data


def page_key(cursor: str | None) -> str:
    return f"page:{cursor or ''}"


def put_page(q: WorkQueue, stage: str, cursor: str | None) -> None:
    key = page_key(cursor)
    if not q.put(stage, key, {"cursor": cursor or ""}):
        q.requeue(stage, [key])  # a relist or restart walks pages that are already done


def record_cursor(q: WorkQueue, item_key: str, next_cursor: str) -> None:
    """Remember that page `item_key` produced `next_cursor`; raise `PaginationLoop` when another page already did.
    A page rerun after a crash produces the same cursor again, which is fine."""
    seen = _seen_cursors(q.get_meta("seen_cursors"))
    if seen.get(next_cursor, item_key) != item_key:
        raise PaginationLoop(f"cursor repeated: {next_cursor}")
    seen[next_cursor] = item_key
    q.set_meta("seen_cursors", json.dumps(seen))


def restart_listing(q: WorkQueue, stage: str) -> None:
    """List again from page 1 (a relist of a finished listing). `list_done` is cleared, so an interrupted relist
    resumes on the next run instead of starting over."""
    q.set_meta("list_done", "")
    q.set_meta("seen_cursors", "{}")
    put_page(q, stage, None)


class FirstClaim:
    """Tells the first page a run's list stage claims from the rest. Only that page's cursor can have been
    carried over from an earlier run (and expired since); build one per run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def __call__(self) -> bool:
        """True on the run's first call, False after."""
        with self._lock:
            first, self._claimed = not self._claimed, True
        return first


def restart_expired_cursor(q: WorkQueue, stage: str, error: Exception) -> dict:
    """A cursor carried over from an earlier run was refused: list again from page 1. Returns the page's result.
    Call it only for the run's first claimed page (`FirstClaim`) with a cursor; any other refused cursor is an
    ordinary failure of its page."""
    log.info("%s: cursor expired, restarting listing (%s)", stage, error)
    q.set_meta("seen_cursors", "{}")
    put_page(q, stage, None)
    return {"restarted": True}
