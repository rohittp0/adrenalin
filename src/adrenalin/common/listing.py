"""Cursor-paginated listings on a WorkQueue: one queue item per page, and loop detection across reruns."""
from __future__ import annotations

import json

from adrenalin.common.pipeline import WorkQueue


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
