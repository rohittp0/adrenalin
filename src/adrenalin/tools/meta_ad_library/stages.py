"""Queue stages of a meta-ad-library scrape: list, detail, fetch and ocr (ADR 0001, ADR 0004).

One queue item per row: the key is the Creative's Library ID, or `<library_id>:<n>` with `variations` (n is the
1-based Variation). Every row passes through `fetch`, media or not, so Export reads its rows from `row_items`
(the `fetch` items, plus rows held in `EXPIRED` during an unfinished relist): the payload holds the Creative and its Variation (`row_from_payload`) plus `eu_reach` when the detail stage
ran; the result holds `image_files`, paths relative to the entity folder. `ocr` items add `image_text`.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

import requests

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.http import PacedSession
from adrenalin.common.listing import page_key, put_page, record_cursor
from adrenalin.common.ocr import OcrResult, recognize
from adrenalin.common.output import write_bytes_atomic
from adrenalin.common.pipeline import Item, Stage, WorkQueue
from adrenalin.tools.meta_ad_library.client import MetaClient, MetaQuery
from adrenalin.tools.meta_ad_library.parse import Card, DocNotFound, GraphQLError, MetaCreative, Variation

log = logging.getLogger(__name__)

STAGE_NAMES = ("list", "detail", "fetch", "ocr")
# Pass as `Pipeline(..., fatal=FATAL)`: a changed doc_id fails every request, so the run stops at the first one.
FATAL = (DocNotFound,)
# A queue stage no worker claims: rows whose media URLs expired (fbcdn URLs are signed and expire), held out of
# `fetch` until the relist gives them fresh URLs, so no worker downloads the expired ones again meanwhile.
EXPIRED = "expired_media"
# fetch payload flag: a relist did not list the Creative again, so its URLs can't be refreshed; it never
# triggers another relist
RELISTED = "relisted"
OCR_METHOD = "ocr"  # ExtractionCache method of `fbimg:<file stem>` entries
_EXPIRED_STATUSES = ("HTTP 403", "HTTP 410")

# (concurrency, throttle_on, max_ahead)
_STAGE_CONFIG = {
    "list": (1, ("detail", "fetch", "ocr"), 200),
    "detail": (1, ("fetch",), 50),
    "fetch": (4, ("ocr",), 50),
    "ocr": (2, (), 0),
}


# -- queue payloads -------------------------------------------------------------


def creative_to_dict(c: MetaCreative) -> dict:
    """A JSON-able dict of a Creative (dates as ISO strings); `creative_from_dict` is the inverse."""
    d = asdict(c)
    d["start_date"] = c.start_date.isoformat()
    d["end_date"] = c.end_date.isoformat() if c.end_date else None
    return d


def _variation_from_dict(d: dict) -> Variation:
    return Variation(**{**d, "image_urls": tuple(d["image_urls"]), "video_urls": tuple(d["video_urls"]),
                        "video_preview_urls": tuple(d["video_preview_urls"]),
                        "cards": tuple(Card(**c) for c in d["cards"])})


def creative_from_dict(d: dict) -> MetaCreative:
    return MetaCreative(**{**d, "start_date": date.fromisoformat(d["start_date"]),
                           "end_date": date.fromisoformat(d["end_date"]) if d["end_date"] else None,
                           "platforms": tuple(d["platforms"]),
                           "variations": tuple(_variation_from_dict(v) for v in d["variations"])})


def row_key(library_id: str, n: int, variations: bool) -> str:
    return f"{library_id}:{n}" if variations else library_id


def row_payload(c: MetaCreative, n: int) -> dict:
    """The queue payload of Variation `n` (1-based) of a Creative: the Creative carrying only that Variation."""
    return {"creative": creative_to_dict(replace(c, variations=(c.variations[n - 1],))), "variation": n}


def row_from_payload(payload: dict) -> tuple[MetaCreative, Variation, int]:
    """(the Creative, carrying only the row's Variation; that Variation; its 1-based index)."""
    c = creative_from_dict(payload["creative"])
    return c, c.variations[0], payload["variation"]


def media_urls(v: Variation) -> list[str]:
    """Image and video preview URLs of a Variation and its Cards, one per media file."""
    by_file: dict[str, str] = {}
    for url in (*v.image_urls, *v.video_preview_urls, *(card.image_url for card in v.cards if card.image_url)):
        by_file.setdefault(media_file(url), url)
    return list(by_file.values())


def media_file(url: str) -> str:
    """Entity-relative path of a downloaded media file. The query string (fbcdn's expiring signature) is left
    out of the name, so a refreshed URL maps to the file already on disk."""
    return f"images/{hashlib.sha1(url.split('?', 1)[0].encode()).hexdigest()[:20]}.jpg"


def row_items(q: WorkQueue) -> list[Item]:
    """Every row of the run, whatever its status: the `fetch` items and the rows waiting in `EXPIRED` for a relist
    (an interrupted relist leaves some there)."""
    return q.items("fetch") + q.items(EXPIRED)


def _expired(error: str | None) -> bool:
    return bool(error) and any(s in error for s in _EXPIRED_STATUSES)


# -- stages ---------------------------------------------------------------------


@dataclass
class MetaContext:
    client: MetaClient
    session: PacedSession  # downloads media
    query: MetaQuery
    entity: Path  # media files go under `entity/images/`, shared by all runs of the Page
    run: Path
    cache: ExtractionCache
    variations: bool = False  # one row per Variation, else Variation 1 only
    eu_reach: bool = False  # the detail stage: one request per Creative
    ocr: bool = False  # the ocr stage reads text in the media files
    today: Callable[[], date] = date.today
    recognize: Callable[[Path], OcrResult] = recognize


class MetaStages:
    def __init__(self, ctx: MetaContext):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._listed = False  # whether this run's list stage has claimed a page yet

    def stages(self, q: WorkQueue, only: str | None = None) -> list[Stage]:
        """The run's stages. `only`: None (all), "list" (list, detail), "fetch" (fetch, ocr) or "export" (none).
        `detail` runs only with `eu_reach`, `ocr` only with `ocr`."""
        names = {None: STAGE_NAMES, "list": ("list", "detail"), "fetch": ("fetch", "ocr"), "export": ()}[only]
        names = tuple(n for n in names if (n != "detail" or self.ctx.eu_reach) and (n != "ocr" or self.ctx.ocr))
        workers = {"list": self.list_page, "detail": self.detail, "fetch": self.fetch, "ocr": self.ocr}
        out = []
        for name in names:
            concurrency, throttle_on, max_ahead = _STAGE_CONFIG[name]
            out.append(Stage(name, workers[name], concurrency, tuple(t for t in throttle_on if t in names),
                             max_ahead))
        return out

    def prepare(self, q: WorkQueue, only: str | None = None) -> None:
        """Run before each run's pipeline (not for `--stage export`), with the same `only` as `stages`. Queues
        page 1 on a new queue.

        Expired media: when the listing is done and rows failed with HTTP 403/410, their URLs expired. They move
        to `EXPIRED` and the listing starts again from page 1; the relist puts them back in `fetch` with fresh
        URLs. Needs the list stage, so `only="fetch"` leaves them for a later run.
        With `ocr`, done rows that have no ocr item yet (an earlier run without `ocr`) get one."""
        if only == "export":
            return
        listing = only in (None, "list")
        if listing:
            q.put("list", page_key(None), {"cursor": ""})  # a new queue starts at page 1; a used one keeps its pages
        if listing and q.get_meta("list_done"):
            keys = [it.key for it in q.items("fetch")
                    if it.status != "done" and _expired(it.error) and not it.payload.get(RELISTED)]
            if keys:
                log.info("fetch: media URLs of %d row(s) expired; listing again from page 1 for fresh ones",
                         len(keys))
                q.move("fetch", EXPIRED, keys)
                q.set_meta("list_done", "")
                q.set_meta("seen_cursors", "{}")
                put_page(q, "list", None)
        if self.ctx.ocr and only in (None, "fetch"):
            for key, result in q.results("fetch"):
                if (result or {}).get("image_files") and not q.has("ocr", key):
                    q.put("ocr", key, {"image_files": result["image_files"]})

    # -- list -----------------------------------------------------------------

    def list_page(self, item: Item, q: WorkQueue) -> dict:
        ctx = self.ctx
        cursor = item.key.removeprefix("page:") or None
        with self._lock:
            resumed, self._listed = not self._listed, True
        try:
            page = ctx.client.search(ctx.query, cursor)
        except DocNotFound:
            raise  # a changed query, not an expired cursor: the pipeline stops the run
        except GraphQLError as e:
            # Only a cursor carried over from an earlier run can have expired; an error on a cursor obtained in
            # this run fails the item normally (retried on the next run).
            if not (cursor and resumed):
                raise
            log.info("list: cursor expired, restarting listing (%s)", e)
            q.set_meta("seen_cursors", "{}")
            put_page(q, "list", None)
            return {"restarted": True}

        for c in page.creatives:
            for n in range(1, (len(c.variations) if ctx.variations else 1) + 1):
                self._queue_row(q, row_key(c.library_id, n, ctx.variations), row_payload(c, n))
        if page.skipped:
            log.warning("list: skipped %d malformed Creative(s) on %s", page.skipped, item.key)
        if cursor and not page.creatives:
            log.warning("list: %s returned no Creatives", item.key)

        nxt = page.next_cursor
        if nxt:
            record_cursor(q, item.key, nxt)
            put_page(q, "list", nxt)
        else:
            self._return_unlisted(q)
            q.set_meta("list_done", ctx.today().isoformat())
        return {"creatives": len(page.creatives), "inactive": sum(not c.is_active for c in page.creatives),
                "next_cursor": nxt}

    def _queue_row(self, q: WorkQueue, key: str, payload: dict) -> None:
        """Queue a listed row to `detail` (with `eu_reach`) or `fetch`. A relist refreshes the payload of a queued
        row (fbcdn URLs expire); a row whose detail is done is refreshed in `fetch` directly, keeping its EU reach."""
        if self.ctx.eu_reach:
            d = q.item("detail", key)
            if d is None or d.status != "done":
                q.put("detail", key, payload, update_payload=True)
                return
            payload = {**payload, "eu_reach": (d.result or {}).get("eu_reach")}
        q.put("fetch", key, payload, update_payload=True)
        if q.has(EXPIRED, key):
            q.move(EXPIRED, "fetch", [key])  # fetch keeps the fresh item just put; the expired one is dropped

    def _return_unlisted(self, q: WorkQueue) -> None:
        """At the end of a listing, rows still in `EXPIRED` were not listed again: back to `fetch` (they stay
        rows of the export), flagged so they never trigger another relist."""
        held = q.items(EXPIRED)
        if not held:
            return
        log.info("list: %d row(s) with expired media were not listed again; their media stay missing", len(held))
        for it in held:
            q.put(EXPIRED, it.key, {**it.payload, RELISTED: True}, update_payload=True)
        q.move(EXPIRED, "fetch", [it.key for it in held])

    # -- detail ---------------------------------------------------------------

    def detail(self, item: Item, q: WorkQueue) -> dict:
        """The Creative's EU reach (a dict of `EuReach`, or None), stored and passed on to `fetch`."""
        reach = self.ctx.client.detail(self.ctx.query, item.payload["creative"]["library_id"])
        data = asdict(reach) if reach is not None else None
        q.put("fetch", item.key, {**item.payload, "eu_reach": data}, update_payload=True)
        return {"eu_reach": data}

    # -- fetch ----------------------------------------------------------------

    def _download(self, url: str) -> bytes:
        resp = self.ctx.session.get(url, allow_redirects=True, retry_network=2)
        if resp.status_code != 200:
            # "HTTP 403"/"HTTP 410" in the item's error is what `prepare` takes for expired URLs
            raise requests.HTTPError(f"HTTP {resp.status_code} for {url.split('?', 1)[0]}", response=resp)
        return resp.content

    def fetch(self, item: Item, q: WorkQueue) -> dict:
        """Download the row's media files (skipping files already on disk). Result: `image_files`, paths
        relative to the entity folder, in the order of `media_urls`."""
        _, variation, _ = row_from_payload(item.payload)
        files = []
        for url in media_urls(variation):
            rel = media_file(url)
            path = self.ctx.entity / rel
            if not path.exists():
                write_bytes_atomic(path, self._download(url))
            files.append(rel)
        if self.ctx.ocr and files:
            q.put("ocr", item.key, {"image_files": files}, update_payload=True)
        return {"image_files": files}

    # -- ocr ------------------------------------------------------------------

    def _lines(self, path: Path) -> list[str]:
        key = f"fbimg:{path.stem}"
        if (hit := self.ctx.cache.get(key, OCR_METHOD)) is not None:
            return hit["lines"]
        res = self.ctx.recognize(path)
        lines = [s for line in res.lines if (s := line.text.strip())]
        self.ctx.cache.put(key, OCR_METHOD, "", {"lines": lines, "engine": res.method})
        return lines

    def ocr(self, item: Item, q: WorkQueue) -> dict:
        """Text in the row's media files: `image_text` joins lines with " | " and images with " || ", one part
        per file of `image_files` (empty for an image without text)."""
        texts = [" | ".join(self._lines(self.ctx.entity / rel)) for rel in item.payload["image_files"]]
        return {"image_text": " || ".join(texts)}
