"""Queue stages of a meta-ad-library scrape: list, detail, fetch and ocr (ADR 0001, ADR 0004).

One queue item per row: the key is the Creative's Library ID, or `<library_id>:<n>` with `variations` (n is the
1-based Variation). Every row passes through `fetch`, media or not, so Export reads its rows from `row_items`
(the `fetch` items, plus rows held in `EXPIRED` during an unfinished relist): the payload holds the Creative and
its Variation (`row_from_payload`) plus `eu_reach` when the detail stage ran; the result holds `image_files`, paths
relative to the entity folder. `ocr` items add `image_text`. With `eu_reach`, a row whose detail has not reached
`fetch` (pending, failed or blocked) is still a row, from its `detail` item.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.http import PacedSession, get_ok
from adrenalin.common.listing import (
    FirstClaim,
    page_key,
    put_page,
    record_cursor,
    restart_expired_cursor,
    restart_listing,
)
from adrenalin.common.ocr import OcrResult, recognize
from adrenalin.common.output import write_bytes_atomic
from adrenalin.common.pipeline import Item, Stage, WorkQueue
from adrenalin.tools.meta_ad_library.client import MetaClient, MetaQuery
from adrenalin.tools.meta_ad_library.hosts import LIBRARY_HOST
from adrenalin.tools.meta_ad_library.parse import Card, DocNotFound, EuReach, GraphQLError, MetaCreative, Variation

log = logging.getLogger(__name__)

STAGE_NAMES = ("list", "detail", "fetch", "ocr")
# Pass as `Pipeline(..., fatal=FATAL)`: a changed doc_id fails every request, so the run stops at the first one.
FATAL = (DocNotFound,)
# A queue stage no worker claims: rows whose media URLs expired (fbcdn URLs are signed and expire), held out of
# `fetch` until the relist gives them fresh URLs, so no worker downloads the expired ones again meanwhile.
EXPIRED = "expired_media"
# payload flag: the row's media expired and a relist has already tried to refresh its URLs (fresh ones, or none
# when the Creative was not listed again). A row with it never triggers another relist, so media that keep
# failing cost one relist, not one per run. An ordinary listing of the row writes a payload without it.
RELISTED = "relisted"
OCR_METHOD = "ocr"  # ExtractionCache method of `fbimg:<file stem>` entries
_EXPIRED_STATUSES = ("HTTP 403", "HTTP 410")
# Detail requests answered with GraphQL errors in a row before the detail stage takes them for a block (ADR 0004):
# Meta's refusal of a logged-out client under load is unverified, and an errors-only answer may be one.
DETAIL_ERRORS_BLOCK = 3

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
    """Every row of the run, whatever its status: the `fetch` items, the rows waiting in `EXPIRED` for a relist
    (an interrupted relist leaves some there), and the `detail` items not in either (their detail is pending,
    failed or blocked: no EU reach and no media files yet)."""
    rows = q.items("fetch") + q.items(EXPIRED)
    have = {it.key for it in rows}
    return rows + [it for it in q.items("detail") if it.key not in have]


def _expired(error: str | None) -> bool:
    return bool(error) and any(s in error for s in _EXPIRED_STATUSES)


# -- stages ---------------------------------------------------------------------


@dataclass
class MetaContext:
    client: MetaClient
    session: PacedSession  # downloads media
    query: MetaQuery
    entity: Path  # media files go under `entity/images/`, shared by all runs of the Page
    cache: ExtractionCache
    variations: bool = False  # one row per Variation, else Variation 1 only
    eu_reach: bool = False  # the detail stage: one request per Creative, shared by its Variations
    ocr: bool = False  # the ocr stage reads text in the media files
    today: Callable[[], date] = date.today
    recognize: Callable[[Path], OcrResult] = recognize


class MetaStages:
    """The stages of one run. Build a fresh one for every run: `list_page` treats the first page it claims as
    carried over from an earlier run (whose cursor may have expired)."""

    def __init__(self, ctx: MetaContext):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._first_claim = FirstClaim()  # a carried-over cursor can only be on the run's first page
        self._detail_errors = 0  # detail requests answered with GraphQL errors in a row

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
        page 1 on a new queue; relists a finished listing (`_relist`, needs the list stage, so `only="fetch"`
        leaves it for a later run).

        With `eu_reach` and the list stage, rows listed without it (an earlier run without `--eu-reach`) get a
        detail item. With `ocr`, done rows that have no ocr item yet (an earlier run without `ocr`) get one."""
        if only == "export":
            return
        listing = only in (None, "list")
        if listing:
            q.put("list", page_key(None), {"cursor": ""})  # a new queue starts at page 1; a used one keeps its pages
            if done := q.get_meta("list_done"):
                self._relist(q, date.fromisoformat(done))
            if self.ctx.eu_reach:
                self._queue_missing_detail(q)
        if self.ctx.ocr and only in (None, "fetch"):
            for key, result in q.results("fetch"):
                if (result or {}).get("image_files") and not q.has("ocr", key):
                    q.put("ocr", key, {"image_files": result["image_files"]})

    def _relist(self, q: WorkQueue, done: date) -> None:
        """List a finished listing again from page 1 when:

        - the Shown period is still open (no end, or an end on or after the last listing) and the last listing
          finished before today: Creatives start and stop every day. At most one relist a day.
        - rows' media URLs expired (fetch failed with HTTP 403/410: fbcdn URLs are signed and expire) and a relist
          has not already tried to refresh them (`RELISTED`).

        A relist moves every row with expired media to `EXPIRED`, so no worker downloads the expired URLs again
        meanwhile; it puts them back in `fetch` with fresh URLs, flagged `RELISTED`. Done rows keep their media
        files and EU reach: the relist refreshes their payloads only."""
        expired = [it for it in q.items("fetch") if it.status != "done" and _expired(it.error)]
        stale = sum(not it.payload.get(RELISTED) for it in expired)
        end = self.ctx.query.end
        daily = (end is None or end >= done) and done < self.ctx.today()
        if not (stale or daily):
            return
        if stale:
            log.info("fetch: media URLs of %d row(s) expired; listing again from page 1 for fresh ones", stale)
        else:
            log.info("list: Shown period still open, last listed %s; listing again from page 1", done.isoformat())
        if expired:
            q.move("fetch", EXPIRED, [it.key for it in expired])
        restart_listing(q, "list")

    @staticmethod
    def _queue_missing_detail(q: WorkQueue) -> None:
        """A detail item for each row without EU reach (listed by a run without `eu_reach`). Sibling Variations
        all get one, and the detail stage still makes one request per Creative (`_sibling_reach`). A row whose
        detail is already done gets its EU reach without a request."""
        details = {d.key: d for d in q.items("detail")}
        for it in q.items("fetch"):
            if "eu_reach" in it.payload:
                continue
            d = details.get(it.key)
            if d is None:
                q.put("detail", it.key, it.payload)
            elif d.status == "done":
                q.put("fetch", it.key, {**it.payload, "eu_reach": (d.result or {}).get("eu_reach")},
                      update_payload=True)

    # -- list -----------------------------------------------------------------

    def list_page(self, item: Item, q: WorkQueue) -> dict:
        ctx = self.ctx
        cursor = item.key.removeprefix("page:") or None
        resumed = self._first_claim()
        try:
            page = ctx.client.search(ctx.query, cursor)
        except DocNotFound:
            raise  # a changed query, not an expired cursor: the pipeline stops the run
        except GraphQLError as e:
            # Only a cursor carried over from an earlier run can have expired; an error on a cursor obtained in
            # this run fails the item normally (retried on the next run).
            if not (cursor and resumed):
                raise
            return restart_expired_cursor(q, "list", e)

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
            if not row_items(q):
                log.warning("no Creatives listed for Page %s in this Region/Shown period; check the Page ID "
                            "(an Ad Library ID of a single ad is not a Page ID)", ctx.query.page_id)
            q.set_meta("list_done", ctx.today().isoformat())
        return {"creatives": len(page.creatives), "inactive": sum(not c.is_active for c in page.creatives),
                "next_cursor": nxt}

    def _queue_row(self, q: WorkQueue, key: str, payload: dict) -> None:
        """Queue a listed row to `detail` (with `eu_reach`) or `fetch`. A relist refreshes the payload of a queued
        row (fbcdn URLs expire) and never redoes done work: a row whose detail is done is refreshed in `fetch`
        directly, keeping its EU reach (with or without `eu_reach` in this run), and a done `fetch` item stays done.
        A row held in `EXPIRED` gets its fresh URLs flagged `RELISTED`; its held copy is dropped once the fresh row
        is in `fetch` (here, or by `detail` when detail has not run for it yet)."""
        held = q.has(EXPIRED, key)
        if held:
            payload = {**payload, RELISTED: True}
        d = q.item("detail", key)
        detail_done = d is not None and d.status == "done"
        if self.ctx.eu_reach and not detail_done:
            q.put("detail", key, payload, update_payload=True)
            return
        if detail_done:
            payload = {**payload, "eu_reach": (d.result or {}).get("eu_reach")}
        q.put("fetch", key, payload, update_payload=True)
        if held:
            q.move(EXPIRED, "fetch", [key])  # fetch keeps the fresh item just put; the expired one is dropped

    def _return_unlisted(self, q: WorkQueue) -> None:
        """At the end of a listing, rows still in `EXPIRED` were not listed again: back to `fetch` (they stay
        rows of the export), flagged so they never trigger another relist. A held row waiting for `detail` was
        listed again: `detail` puts its fresh row in `fetch`, so its held copy stays until then."""
        held = [it for it in q.items(EXPIRED)
                if (d := q.item("detail", it.key)) is None or d.status == "done"]
        if not held:
            return
        log.info("list: %d row(s) with expired media were not listed again; their media stay missing", len(held))
        for it in held:
            q.put(EXPIRED, it.key, {**it.payload, RELISTED: True}, update_payload=True)
        q.move(EXPIRED, "fetch", [it.key for it in held])

    # -- detail ---------------------------------------------------------------

    def detail(self, item: Item, q: WorkQueue) -> dict:
        """The Creative's EU reach (a dict of `EuReach`, or None), stored and passed on to `fetch`. One request per
        Creative: with `variations`, a sibling row's done result is reused."""
        library_id = item.payload["creative"]["library_id"]
        found, data = self._sibling_reach(q, library_id, item.key)
        if not found:
            reach = self._request_detail(library_id)
            data = asdict(reach) if reach is not None else None
        q.put("fetch", item.key, {**item.payload, "eu_reach": data}, update_payload=True)
        if q.has(EXPIRED, item.key):
            q.move(EXPIRED, "fetch", [item.key])  # the row was held for a relist: drop its expired copy
        return {"eu_reach": data}

    def _request_detail(self, library_id: str) -> EuReach | None:
        """One detail request. `DETAIL_ERRORS_BLOCK` GraphQL errors in a row (not `DocNotFound`) are taken for a
        block of the Ad Library host: it goes into the shared cooldown and `Blocked` stops the stage, so the run
        ends cleanly and a rerun resumes after the cooldown. Each error before that fails only its item."""
        try:
            reach = self.ctx.client.detail(self.ctx.query, library_id)
        except DocNotFound:
            raise
        except GraphQLError as e:
            with self._lock:
                self._detail_errors += 1
                n = self._detail_errors
                if n >= DETAIL_ERRORS_BLOCK:
                    self._detail_errors = 0
            if n >= DETAIL_ERRORS_BLOCK:
                blocked = self.ctx.session.block(
                    LIBRARY_HOST, f"{n} detail requests in a row answered with GraphQL errors (latest: {e})")
                if blocked is not None:
                    raise blocked from e
            raise
        with self._lock:
            self._detail_errors = 0
        return reach

    @staticmethod
    def _sibling_reach(q: WorkQueue, library_id: str, key: str) -> tuple[bool, dict | None]:
        """(found, EU reach) from a done detail item of another Variation of the same Creative. The list stage
        queues all Variations of a Creative together, so they are `<library_id>:1..n` without gaps."""
        if key == library_id:
            return False, None  # without `variations` a Creative has one row
        n = 1
        while (d := q.item("detail", row_key(library_id, n, True))) is not None:
            if d.key != key and d.status == "done" and "eu_reach" in (d.result or {}):
                return True, d.result["eu_reach"]
            n += 1
        return False, None

    # -- fetch ----------------------------------------------------------------

    def fetch(self, item: Item, q: WorkQueue) -> dict:
        """Download the row's media files (skipping files already on disk). Result: `image_files`, paths
        relative to the entity folder, in the order of `media_urls`."""
        _, variation, _ = row_from_payload(item.payload)
        files = []
        for url in media_urls(variation):
            rel = media_file(url)
            path = self.ctx.entity / rel
            if not path.exists():
                # "HTTP 403"/"HTTP 410" in the item's error is what `_relist` takes for expired URLs; the message
                # leaves out fbcdn's signature
                write_bytes_atomic(path, get_ok(self.ctx.session, url, retry_network=2,
                                                shown_url=url.split("?", 1)[0]).content)
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
