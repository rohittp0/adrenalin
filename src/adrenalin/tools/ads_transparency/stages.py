"""Queue stages of an ads-transparency scrape: list, sample, detail, fetch and extract (ADR 0001, ADR 0003)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.http import Blocked, PacedSession
from adrenalin.common.ocr import recognize
from adrenalin.common.ocr.codex import CodexRunner
from adrenalin.common.pipeline import Item, Stage, WorkQueue
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.copy import MIN_CONFIDENCE, AdCopy, extract_image, parse_content_preview
from adrenalin.tools.ads_transparency.rpc import (
    ListedCreative,
    PaginationLoop,
    Preview,
    Query,
    TransparencyClient,
    preview_ids,
)

# fetch downloads images; content.js Previews go on to `script`, which has its own (slower) host, so a
# block there stops only script downloads. `sample` admits held Creatives into the Copy sample in rounds (ADR 0003);
# `held` is a queue stage no worker claims: listed Creatives left out of the sample so far.
STAGE_NAMES = ("list", "sample", "detail", "fetch", "script", "extract")
HELD = "held"
ROUND_BATCH = 50  # held Creatives a sampling round admits by novelty, once every group has its minimum
OEMBED_URL = "https://www.youtube.com/oembed"
CONTENT_DIR = "content"

# (concurrency, throttle_on, max_ahead), from the plan's backpressure limits.
# A stage that throttles on a blocked stage stops with it (see Pipeline): list and detail stop when script is
# blocked, while fetch, which doesn't wait on script, keeps downloading images.
_STAGE_CONFIG = {
    "list": (1, ("detail", "fetch", "script", "extract"), 200),
    # a round decides on complete reads only, so it waits until every admitted Creative has been read
    "sample": (1, ("detail", "fetch", "script", "extract"), 0),
    "detail": (1, ("fetch", "script", "extract"), 50),
    "fetch": (4, ("extract",), 50),
    "script": (1, ("extract",), 50),
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


COPY_STATUSES = ("read", "not_sampled", "pending", "no_copy", "preview_broken", "unreadable")


def copy_status(extract_status: str | None, result: dict | None, held: bool = False) -> str:
    """Why a row has or lacks copy. `extract_status` is the extract item's status (None: no item yet);
    `held` is a Creative left out of the sample. `result["format"]` is the Creative's format, when known."""
    if held:
        return "not_sampled"
    if extract_status != "done":
        return "pending"
    r = result or {}
    if r.get("preview_broken"):
        return "preview_broken"
    if r.get("content_unparsed"):
        return "unreadable"
    if any(r.get(k) for k in ("brand", "headline", "description", "display_url")):
        return "read"
    if r.get("vision_pending"):
        return "pending"  # the Codex read was deferred (cap or breaker), so "no copy" is not known yet
    if r.get("preview_kind") == "content_js":
        return "no_copy"
    if r.get("preview_kind") == "image" and not r.get("full_text") and r.get("format") in ("image", "video"):
        return "no_copy"
    return "unreadable"


# -- Copy sample (ADR 0003) ---------------------------------------------------

def listing_stage(variations: bool) -> str:
    """Where an admitted Creative goes: `detail` lists its Variations first, otherwise straight to `fetch`."""
    return "detail" if variations else "fetch"


def listing_key(creative_id: str, variations: bool) -> str:
    """A Creative's key in `held` and in its listing stage (the same, so `WorkQueue.move` can admit it)."""
    return creative_id if variations else f"{creative_id}#1"


def sample_group(payload: dict, query: Query, today: date) -> str:
    """The Copy sample's allocation unit: launch period x running/stopped bucket, and the Ads account that the
    listed Preview exposes ("" when it exposes none). `payload` is a listing payload (held, detail or fetch)."""
    p = payload.get("preview") or {}
    account = preview_ids(p.get("url", ""))[0] if p.get("kind") == "content_js" else ""
    bucket = sampling.bucket_key(date.fromisoformat(payload["first_shown"]),
                                 date.fromisoformat(payload["last_shown"]), query.start, query.end, today)
    return f"{bucket}|{account}"


def message_of(extract_status: str | None, result: dict | None, fmt: str) -> str:
    """The Message a read counts toward Coverage; "" unless the copy was read with enough confidence."""
    r = result or {}
    if copy_status(extract_status, {**r, "format": fmt}) != "read":
        return ""
    conf = r.get("confidence")
    if conf is not None and conf < MIN_CONFIDENCE:
        return ""  # OCR misreads would look like new Messages
    return sampling.normalize_message(r.get("headline") or "", r.get("description") or "")


@dataclass
class SampleGroup:
    held: list[str] = field(default_factory=list)  # held keys, in admission-hash order
    admitted: int = 0  # Creatives sent to reading (read, pending or failed)
    processed: int = 0  # Creatives whose copy reading finished (copy status neither pending nor not_sampled)
    messages: list[str] = field(default_factory=list)  # one per processed Creative (Variation 1); "" if none

    @property
    def inventory(self) -> int:
        return self.admitted + len(self.held)


def _admission_rank(key: str) -> str:
    return hashlib.sha1(key.partition("#")[0].encode()).hexdigest()


def sample_groups(q: WorkQueue, variations: bool, query: Query, today: date) -> tuple[dict[str, SampleGroup],
                                                                                     dict[str, str]]:
    """Per-group Inventory, reads and Messages of the run, counted per Creative (Variation 1 only), and each
    Creative's group."""
    groups: dict[str, SampleGroup] = {}
    of_creative: dict[str, str] = {}
    extracted = dict(q.results("extract"))
    for it in q.items(listing_stage(variations)):
        cr = it.payload["creative_id"]
        if cr in of_creative:
            continue
        g = groups.setdefault(of_creative.setdefault(cr, sample_group(it.payload, query, today)), SampleGroup())
        g.admitted += 1
        key = f"{cr}#1"
        status = "done" if key in extracted else None
        result = extracted.get(key)
        if copy_status(status, {**(result or {}), "format": it.payload.get("format", "")}) != "pending":
            g.processed += 1
            g.messages.append(message_of(status, result, it.payload.get("format", "")))
    for it in sorted(q.items(HELD), key=lambda i: _admission_rank(i.key)):
        cr = it.payload["creative_id"]
        if cr in of_creative:
            continue  # already admitted (a stale held copy); never count a Creative twice
        g = groups.setdefault(of_creative.setdefault(cr, sample_group(it.payload, query, today)), SampleGroup())
        g.held.append(it.key)
    return groups, of_creative


def seed_round(q: WorkQueue) -> None:
    """Queue the next sampling round unless one is already waiting. Round keys are never reused, since a done
    `round:N` can't be put again."""
    if q.backlog("sample") == 0:
        q.put("sample", f"round:{_rounds(q)}", {})


def _rounds(q: WorkQueue) -> int:
    return sum(n for (stage, _), n in q.counts().items() if stage == "sample")


def sampling_meta(q: WorkQueue) -> dict:
    """The `sampling` meta: admission probability `p` (written once), and the run's Coverage `target` and
    `max_ads`."""
    return json.loads(q.get_meta("sampling") or "{}")


@dataclass
class _Recovered:
    n: int  # 1-based index of the Variation in the detail response
    alt: Preview
    rel: str  # run-relative image path


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
    # Copy sample: read every Creative, the Coverage target, and an optional cap on Creatives read
    read_all: bool = False
    coverage: float = 0.95
    max_ads: int | None = None


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
        elif only == "fetch":
            names = ("fetch", "script")
        elif only == "extract":
            names = ("extract",)
        else:
            names = STAGE_NAMES  # `sample` runs only here: it admits by Coverage, so it needs reads to happen
        names = tuple(n for n in names if n != "detail" or self.ctx.variations)
        workers = {"list": self.list_page, "sample": self.sample, "detail": self.detail, "fetch": self.fetch,
                   "script": self.script, "extract": self.extract}
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

        estimate = page.count_high or page.count_low
        p = self._admit_probability(q, estimate)
        target = listing_stage(ctx.variations)
        for c in page.creatives:
            fields = creative_fields(c) | ({} if ctx.variations else {"variation": 1})
            payload = {**fields, "preview": preview_to_dict(c.preview)}
            key = listing_key(c.creative_id, ctx.variations)
            # Admitted at random (the head start), or already admitted by a round and now listed again.
            if q.has(target, key) or sampling.admitted(c.creative_id, p):
                q.put(target, key, payload, update_payload=True)
            else:
                # `bucket` is for inspection; rounds recompute the group, so a relist's new dates count.
                q.put(HELD, key, {**payload, "bucket": sample_group(payload, ctx.query, ctx.today())},
                      update_payload=True)
        if estimate:
            q.set_meta("count_estimate", str(estimate))  # Google's rough total, for progress display
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
            if q.count(HELD, "pending"):
                seed_round(q)  # before this page completes, so the sample stage can't finish first
        return {"creatives": len(page.creatives), "next_cursor": nxt}

    def _admit_probability(self, q: WorkQueue, estimate: int | None) -> float:
        """The run's admission probability: set from the first listed page's estimate, never recomputed."""
        meta = sampling_meta(q)
        if "p" not in meta:
            # Cursor pages may carry no estimate: fall back to one stored from an earlier page.
            estimate = estimate or int(q.get_meta("count_estimate") or 0) or None
            meta["p"] = sampling.admit_probability(estimate, self.ctx.read_all)
            q.set_meta("sampling", json.dumps(meta))
        return meta["p"]

    # -- sample -------------------------------------------------------------

    def sample(self, item: Item, q: WorkQueue) -> dict:
        """One sampling round. It runs once every admitted Creative has been read: it stops at the Coverage target
        (or `max_ads`) once every group has its minimum, otherwise it admits the next held Creatives and queues the
        next round, before this one completes."""
        ctx = self.ctx
        if not q.get_meta("list_done"):
            # A relist is under way: the Inventory is incomplete. list_page queues a new round when it finishes.
            return {"deferred": True}
        groups, _ = sample_groups(q, ctx.variations, ctx.query, ctx.today())
        read = sum(g.admitted for g in groups.values())
        cov = sampling.coverage([m for g in groups.values() for m in g.messages])
        if ctx.read_all:
            chosen = [k for g in groups.values() for k in g.held]
        else:
            # Admitted Creatives count toward the minimum even if their read failed, so a failure can't make the
            # round admit everything; the failed item is retried on the next run.
            quotas_met = all(g.admitted >= min(sampling.MIN_PER_BUCKET, g.inventory) for g in groups.values())
            if sampling.should_stop(cov, read, ctx.coverage, ctx.max_ads, quotas_met):
                self._log_stop(cov, read, sum(g.inventory for g in groups.values()))
                return {"admitted": 0, "read": read, "coverage": cov}
            # Minimums first, alone: novelty is judged only once every group has reads to judge it by.
            batch = 0 if not quotas_met else ROUND_BATCH if ctx.max_ads is None else \
                max(0, min(ROUND_BATCH, ctx.max_ads - read))
            states = {k: sampling.BucketState(g.inventory, g.admitted, g.messages) for k, g in groups.items()}
            plan = sampling.allocate(states, {k: len(g.held) for k, g in groups.items()}, batch)
            chosen = [key for k, n in plan.items() for key in groups[k].held[:n]]
        if not chosen:
            self._log_stop(cov, read, sum(g.inventory for g in groups.values()))
            return {"admitted": 0, "read": read, "coverage": cov}
        q.move(HELD, listing_stage(ctx.variations), chosen)
        # The next round goes in while this one still runs, so the stage never looks finished between rounds.
        q.put("sample", f"round:{_rounds(q)}", {})
        return {"admitted": len(chosen), "read": read, "coverage": cov}

    def _log_stop(self, cov: float | None, read: int, inventory: int) -> None:
        shown = "unknown" if cov is None else f"{cov:.3f}"
        self.ctx.log(f"sample: stopping with Coverage {shown} (target {self.ctx.coverage}); "
                     f"{read:,} of {inventory:,} Creatives in the Copy sample")

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
        """Every listed Variation passes through fetch (Export reads its rows from here). Images are downloaded;
        content.js Previews are handed to the script stage."""
        p = preview_from_dict(item.payload.get("preview"))
        result: dict = {}
        if p is not None and p.kind == "image":
            rel = f"images/{image_name(p)}.png"
            path = self.ctx.entity / rel
            if not path.exists():
                write_atomic(path, self._get_ok(p.url).content)
            result["image_file"] = rel
        elif p is not None:
            q.put("script", item.key, item.payload, update_payload=True)
            return result
        q.put("extract", item.key, {**item.payload, **result}, update_payload=True)
        return result

    def script(self, item: Item, q: WorkQueue) -> dict:
        p = preview_from_dict(item.payload.get("preview"))
        # The script (often 150-450 KB) goes to a file, never into the queue database.
        rel = content_file(item.key)
        write_atomic(self.ctx.run / rel, self._get_ok(p.url).content)
        q.put("extract", item.key, {**item.payload, "content_file": rel}, update_payload=True)
        return {}

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
        unparsed = False
        extra: dict = {}
        if p is None:
            self.warn(f"no preview for {item.key}; copy left blank")
            copy = AdCopy()
        elif p.kind == "content_js":
            copy = parse_content_preview(self._read_js(pl, p), p.url)
            if copy is None:
                kept = f" (kept at {self.ctx.run / pl['content_file']})" if pl.get("content_file") else ""
                self.warn(f"content.js not parsed for {item.key}; copy left blank{kept}")
                copy = AdCopy()
                unparsed = True
            if copy.video_id:
                title, title_pending = self._video_title(copy.video_id)
        else:
            copy = self._read_image(ctx.entity / pl["image_file"], p, pl.get("format", ""))
            if copy.preview_broken and not ctx.variations:  # with variations, sibling rows carry their own copy
                copy, used, unverified = self._recover_broken(item, p, pl.get("format", ""), copy)
                extra = {"variation_used": used.n if used else 0, "broken_unverified": unverified}
                if used:  # the row shows the Variation that was read
                    extra |= {"preview_url": used.alt.url, "image_file": used.rel}
        return {**copy.to_dict(), "video_title": title, "title_pending": title_pending,
                "preview_kind": p.kind if p else "", "content_unparsed": unparsed, **extra}

    def _read_image(self, path: Path, p: Preview, fmt: str) -> AdCopy:
        ctx = self.ctx
        return extract_image(path, image_name(p), fmt, cache=ctx.cache, codex=ctx.codex, force_vision=ctx.vision,
                             ocr=ctx.ocr, warn=self.warn, host_exists=ctx.host_exists)

    def _recover_broken(self, item: Item, p: Preview, fmt: str,
                        broken: AdCopy) -> tuple[AdCopy, _Recovered | None, bool]:
        """The Preview is Google's error page. Read the Creative's other image Variations and use the first that
        is not broken. Returns (copy, the Variation used or None, whether the Variations could not be checked:
        a block, network or RPC error, retried on the next run)."""
        ctx = self.ctx
        try:
            previews = ctx.client.variations(ctx.query.advertiser_id, item.key.split("#")[0])
            for n, alt in enumerate(previews, 1):
                if alt.kind != "image" or image_name(alt) == image_name(p):
                    continue
                rel = f"images/{image_name(alt)}.png"
                path = ctx.entity / rel
                if not path.exists():
                    write_atomic(path, self._get_ok(alt.url).content)
                copy = self._read_image(path, alt, fmt)
                if not copy.preview_broken:
                    return copy, _Recovered(n, alt, rel), False
        except (Blocked, requests.RequestException, ValueError) as e:  # ValueError: RpcError, unparsable detail
            self.warn(f"Broken Preview for {item.key}: could not check its other Variations "
                      f"({type(e).__name__}: {e}); retrying on the next run")
            return broken, None, True
        return broken, None, False

    def retry_titles(self, q: WorkQueue) -> None:
        """Fetch the video titles that failed temporarily on an earlier run, from the stored video ID. Only the
        title changes: the rest of the row is kept and content.js is not needed."""
        for key, r in q.results("extract"):
            if not (r or {}).get("title_pending") or not r.get("video_id"):
                continue
            title, pending = self._video_title(r["video_id"])
            q.complete("extract", key, {**r, "video_title": title, "title_pending": pending})
            if pending:
                return  # YouTube still failing: the rest wait for the next run

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
        """After the extract item is marked done its content.js file is no longer needed, unless it could not be
        parsed: that one is kept for inspection."""
        if (rel := item.payload.get("content_file")) and not (result or {}).get("content_unparsed"):
            (self.ctx.run / rel).unlink(missing_ok=True)
