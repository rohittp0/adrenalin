"""Queue stages of an ads-transparency scrape: list, detail, fetch and extract (ADR 0001)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.http import Blocked, PacedSession
from adrenalin.common.ocr import recognize
from adrenalin.common.ocr.codex import CodexRunner
from adrenalin.common.pipeline import Item, Stage, WorkQueue
from adrenalin.tools.ads_transparency.copy import AdCopy, extract_image, parse_content_js, parse_overlay_param
from adrenalin.tools.ads_transparency.rpc import (
    ListedCreative,
    PaginationLoop,
    Preview,
    Query,
    TransparencyClient,
)

STAGE_NAMES = ("list", "detail", "fetch", "extract")
OEMBED_URL = "https://www.youtube.com/oembed"
CONTENT_DIR = "content"

# (concurrency, throttle_on, max_ahead), from the plan's backpressure limits.
_STAGE_CONFIG = {
    "list": (1, ("detail", "fetch", "extract"), 200),
    "detail": (1, ("fetch", "extract"), 50),
    "fetch": (4, ("extract",), 50),
    "extract": (2, (), 0),
}


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def preview_to_dict(p: Preview | None) -> dict | None:
    return None if p is None else {"kind": p.kind, "url": p.url, "width": p.width, "height": p.height}


def preview_from_dict(d: dict | None) -> Preview | None:
    if not d:
        return None
    return Preview(d["kind"], d["url"], d.get("width"), d.get("height"))


def image_name(p: Preview) -> str:
    return p.image_id or hashlib.sha1(p.url.encode()).hexdigest()[:20]


def creative_fields(c: ListedCreative) -> dict:
    return {
        "creative_id": c.creative_id,
        "format": c.format,
        "first_shown": c.first_shown.date().isoformat(),
        "last_shown": c.last_shown.date().isoformat(),
        "days_shown": c.days_shown,
    }


def _seen_cursors(raw: str | None) -> dict[str, str]:
    """The `seen_cursors` meta: {cursor: page key that produced it}. Older runs stored a list of cursors
    (producer unknown, so any repeat of those is still a loop)."""
    data = json.loads(raw or "{}")
    return {c: "" for c in data} if isinstance(data, list) else data


def page_key(cursor: str | None) -> str:
    return f"page:{cursor or ''}"


def content_file(key: str) -> str:
    """Run-relative path of a fetched content.js, named per queue item so no two items share a file."""
    return f"{CONTENT_DIR}/{hashlib.sha1(key.encode()).hexdigest()}.js"


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: several creatives can share one simgad image.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@dataclass
class StageContext:
    client: TransparencyClient
    session: PacedSession
    query: Query
    entity: Path
    run: Path
    cache: ExtractionCache
    codex: CodexRunner | None
    variations: bool = False
    vision: bool = False
    ocr: Callable = recognize
    log: Callable[[str], None] = print
    today: Callable[[], date] = date.today
    # oEmbed must never wait or back off: a fail-fast session sharing the cooldown (defaults to `session`)
    oembed: PacedSession | None = None
    # verifies domains read by OCR (None: no network check)
    host_exists: Callable[[str], bool] | None = None


class AdsStages:
    def __init__(self, ctx: StageContext):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._oembed_warned = False
        self._listed = False  # whether this run's list stage has claimed a page yet

    def warn(self, message: str) -> None:
        self.ctx.log(f"warning: {message}")

    def stages(self, only: str | None = None) -> list[Stage]:
        if only == "export":
            names: tuple[str, ...] = ()
        elif only == "list":
            names = ("list", "detail")
        elif only in ("fetch", "extract"):
            names = (only,)
        else:
            names = STAGE_NAMES
        names = tuple(n for n in names if n != "detail" or self.ctx.variations)
        workers = {"list": self.list_page, "detail": self.detail, "fetch": self.fetch, "extract": self.extract}
        out = []
        for name in names:
            concurrency, throttle_on, max_ahead = _STAGE_CONFIG[name]
            out.append(Stage(name, workers[name], concurrency,
                             tuple(t for t in throttle_on if t in names), max_ahead,
                             on_done=self.extract_done if name == "extract" else None))
        return out

    # -- list ---------------------------------------------------------------

    @staticmethod
    def _put_page(q: WorkQueue, cursor: str | None) -> None:
        key = page_key(cursor)
        if not q.put("list", key, {"cursor": cursor or ""}):
            q.requeue("list", [key])  # a relist or restart walks pages that are already done

    def list_page(self, item: Item, q: WorkQueue) -> dict:
        ctx = self.ctx
        cursor = item.key.removeprefix("page:") or None
        with self._lock:
            resumed, self._listed = not self._listed, True
        try:
            page = ctx.client.search(ctx.query, cursor)
        except ValueError as e:
            # Only a cursor carried over from an earlier run can have expired; an error on a cursor
            # obtained in this run fails the item normally (retried on the next run).
            if not (cursor and resumed):
                raise
            ctx.log(f"list: cursor expired, restarting listing ({e})")
            q.set_meta("seen_cursors", "{}")
            self._put_page(q, None)
            return {"restarted": True}

        for c in page.creatives:
            fields = creative_fields(c)
            if ctx.variations:
                q.put("detail", c.creative_id, {**fields, "preview": preview_to_dict(c.preview)},
                      update_payload=True)
            else:
                q.put("fetch", f"{c.creative_id}#1",
                      {**fields, "variation": 1, "preview": preview_to_dict(c.preview)}, update_payload=True)
        if page.skipped:
            self.warn(f"skipped {page.skipped} malformed creative(s) on {item.key}")
        if cursor and not page.creatives:
            self.warn(f"{item.key} returned no creatives")

        nxt = page.next_cursor
        if nxt:
            # cursor -> the page that produced it; a page rerun after a crash produces the same cursor again
            seen = _seen_cursors(q.get_meta("seen_cursors"))
            if seen.get(nxt, item.key) != item.key:
                raise PaginationLoop(f"cursor repeated: {nxt}")
            seen[nxt] = item.key
            q.set_meta("seen_cursors", json.dumps(seen))
            self._put_page(q, nxt)
        else:
            q.set_meta("list_done", ctx.today().isoformat())
        return {"creatives": len(page.creatives), "next_cursor": nxt}

    # -- detail -------------------------------------------------------------

    def detail(self, item: Item, q: WorkQueue) -> dict:
        previews: list[Preview | None] = list(
            self.ctx.client.variations(self.ctx.query.advertiser_id, item.key))
        if not previews:
            previews = [preview_from_dict(item.payload.get("preview"))]
        fields = {k: v for k, v in item.payload.items() if k != "preview"}
        for n, p in enumerate(previews, 1):
            q.put("fetch", f"{item.key}#{n}", {**fields, "variation": n, "preview": preview_to_dict(p)})
        return {"variations": len(previews)}

    # -- fetch --------------------------------------------------------------

    def _get_ok(self, url: str) -> requests.Response:
        resp = self.ctx.session.get(url, allow_redirects=True)
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code} for {url}", response=resp)
        return resp

    def fetch(self, item: Item, q: WorkQueue) -> dict:
        p = preview_from_dict(item.payload.get("preview"))
        result: dict = {}
        extra: dict = {}
        if p is not None and p.kind == "image":
            rel = f"images/{image_name(p)}.png"
            path = self.ctx.entity / rel
            if not path.exists():
                write_atomic(path, self._get_ok(p.url).content)
            result["image_file"] = rel
        elif p is not None:
            # The script (often 150-450 KB) goes to a file, never into the queue database.
            rel = content_file(item.key)
            write_atomic(self.ctx.run / rel, self._get_ok(p.url).content)
            extra["content_file"] = rel
        q.put("extract", item.key, {**item.payload, **result, **extra}, update_payload=True)
        return result

    # -- extract ------------------------------------------------------------

    def _video_title(self, video_id: str) -> tuple[str, bool]:
        """The video's title, and whether a failure looks temporary (worth retrying on the next run)."""
        transient = True
        try:
            resp = (self.ctx.oembed or self.ctx.session).get(
                OEMBED_URL, params={"url": watch_url(video_id), "format": "json"}, allow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("title"), str):
                    return data["title"], False
                problem, transient = "no title in response", False
            else:
                problem = f"HTTP {resp.status_code}"
                # 401/403/404: private video or embedding disabled; it won't change on a rerun
                transient = resp.status_code == 429 or resp.status_code >= 500
        except (Blocked, requests.RequestException, ValueError) as e:
            problem = f"{type(e).__name__}: {e}"
        with self._lock:
            first, self._oembed_warned = not self._oembed_warned, True
        if first:
            self.warn(f"YouTube oEmbed failed ({problem}); video_title left blank")
        return "", transient

    def extract(self, item: Item, q: WorkQueue) -> dict:
        ctx = self.ctx
        pl = item.payload
        p = preview_from_dict(pl.get("preview"))
        title, title_pending = "", False
        if p is None:
            self.warn(f"no preview for {item.key}; copy left blank")
            copy = AdCopy()
        elif p.kind == "content_js":
            copy = parse_content_js(self._read_js(pl, p)) or parse_overlay_param(p.url)
            if copy is None:
                self.warn(f"content.js not parsed for {item.key}; copy left blank")
                copy = AdCopy()
            if copy.video_id:
                title, title_pending = self._video_title(copy.video_id)
        else:
            copy = extract_image(
                ctx.entity / pl["image_file"], image_name(p), pl.get("format", ""),
                cache=ctx.cache, codex=ctx.codex, force_vision=ctx.vision, ocr=ctx.ocr, warn=self.warn,
                host_exists=ctx.host_exists,
            )
        return {**copy.to_dict(), "video_title": title, "title_pending": title_pending,
                "preview_kind": p.kind if p else ""}

    def _read_js(self, pl: dict, p: Preview) -> str:
        path = self.ctx.run / pl["content_file"] if pl.get("content_file") else None
        try:
            if path is None:
                raise FileNotFoundError("no content file recorded")
            data = path.read_bytes()
        except FileNotFoundError:
            data = self._get_ok(p.url).content  # file lost (crash, cleanup): download it again
        return data.decode("utf-8", errors="replace")

    def extract_done(self, item: Item, result: dict | None) -> None:
        """After the extract item is marked done its content.js file is no longer needed."""
        if rel := item.payload.get("content_file"):
            (self.ctx.run / rel).unlink(missing_ok=True)
