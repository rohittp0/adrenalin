"""Public API of the ads-transparency tool: `scrape()` one Advertiser × Region × Shown period to CSV."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.geo import region_to_geo_id
from adrenalin.common.http import Cooldown, HostChecker, PacedSession
from adrenalin.common.ocr import recognize
from adrenalin.common.ocr.codex import DEFAULT_VISION_MODEL, CodexRunner
from adrenalin.common.output import cache_dir, entity_dir, write_csv_atomic
from adrenalin.common.pipeline import Pipeline, RunResult, WorkQueue, print_to_stderr
from adrenalin.common.progress import Bar, QueueProgress, stage_bar
from adrenalin.tools.ads_transparency.rpc import (
    HOST,
    ListedCreative,
    Query,
    TransparencyClient,
    preview_ids,
    transparency_url,
)
from adrenalin.tools.ads_transparency.stages import (
    CONTENT_DIR,
    STAGE_NAMES,
    AdsStages,
    StageContext,
    content_file,
    watch_url,
)

TOOL_NAME = "ads-transparency"
RPC_INTERVAL = (6.0, 2.0)
# content.js Previews: this host blocked a run within minutes at 0.5 s, so it is paced like a page view.
SCRIPT_HOST, SCRIPT_INTERVAL = "displayads-formats.googleusercontent.com", (2.5, 0.5)
# simgad images: never blocked so far; still kept to about one a second.
IMAGE_HOST, IMAGE_INTERVAL = "tpc.googlesyndication.com", (1.0, 0.3)

CSV_FIELDS: tuple[str, ...] = (
    "transparency_url", "creative_id", "variation", "format", "region", "first_shown", "last_shown",
    "days_shown", "website", "landing_url", "display_url", "brand", "headline", "description", "cta",
    "sitelinks", "full_text", "video_id", "video_url", "video_title", "preview_url", "image_file",
    "image_width", "image_height", "logo_url", "image_urls", "extraction_method", "extraction_model",
    "extraction_confidence", "copy_status", "sample_weight", "ads_account_id", "ads_account_source", "ad_group_id",
)

COPY_STATUSES = ("read", "not_sampled", "pending", "no_copy", "preview_broken", "unreadable")


@dataclass(frozen=True)
class ScrapeOptions:
    advertiser_id: str
    region: str = "anywhere"
    start: date | None = None
    end: date | None = None
    variations: bool = False
    vision: bool = False
    vision_model: str = DEFAULT_VISION_MODEL
    vision_limit: int = 200
    wait_on_block: bool = False
    stage: Literal["list", "fetch", "extract", "export"] | None = None
    out: Path = Path("out")
    proxies: tuple[str, ...] = ()
    progress: bool = False  # tqdm progress bars on stderr


@dataclass
class ScrapeResult:
    csv_path: Path
    rows: int
    blocked_until: float | None
    failed: dict[str, int]
    vision_skipped: int
    # pipeline-level crashes (not item failures); a non-empty list is a failed run
    errors: list[str] = field(default_factory=list)


def make_session(out: Path, proxies: tuple[str, ...] = (), wait_on_block: bool = False,
                 log: Callable[[str], None] | None = print_to_stderr) -> PacedSession:
    return PacedSession(Cooldown(cache_dir(out) / "cooldown.sqlite3"),
                        {HOST: RPC_INTERVAL, SCRIPT_HOST: SCRIPT_INTERVAL, IMAGE_HOST: IMAGE_INTERVAL},
                        proxies=proxies, wait_on_block=wait_on_block, log=log)


def _query(advertiser_id: str, region: str, start: date | None, end: date | None) -> Query:
    if start and end and start > end:
        raise ValueError(f"start {start} is after end {end}")
    return Query(advertiser_id, region_to_geo_id(region), start, end)


def list_creatives(advertiser_id: str, region: str = "anywhere", start: date | None = None,
                   end: date | None = None, *, session: PacedSession | None = None,
                   out: Path = Path("out")) -> Iterator[ListedCreative]:
    """Every listed Creative of an Advertiser, page by page (no copy extraction)."""
    query = _query(advertiser_id, region, start, end)
    own = session is None
    s = make_session(out) if own else session
    try:
        for page in TransparencyClient(s).iter_pages(query):
            yield from page.creatives
    finally:
        if own:
            s.close()


def _write_json_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _advertiser_name(client: TransparencyClient, query: Query, out: Path) -> str:
    path = cache_dir(out) / "advertisers.json"
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    name = known.get(query.advertiser_id)
    if isinstance(name, str) and name:
        return name
    name = client.advertiser_name(query)
    if not name:
        raise ValueError(f"No creatives found for {query.advertiser_id}")
    known[query.advertiser_id] = name
    _write_json_atomic(path, known)
    return name


def _region_label(region: str) -> str:
    return "anywhere" if region.lower() == "anywhere" else region.upper()


def run_dir_name(options: ScrapeOptions) -> str:
    name = f"{_region_label(options.region)}_{options.start or 'all'}_{options.end or 'all'}"
    return name + ("_variations" if options.variations else "")


def _remove_done_content(q: WorkQueue, run: Path) -> None:
    """Delete content.js files left behind by extract items that already completed."""
    folder = run / CONTENT_DIR
    if not folder.is_dir():
        return
    done = {Path(content_file(key)).name for key, r in q.results("extract") if not (r or {}).get("content_unparsed")}
    for f in folder.iterdir():
        if f.name in done:
            f.unlink(missing_ok=True)


def _prepare_queue(q: WorkQueue, options: ScrapeOptions, log: Callable[[str], None]) -> None:
    # Requeues only when the stage that redoes the work runs: a requeue clears the item's result,
    # so e.g. `--stage export` must not blank copy it cannot re-extract.
    if not q.put("list", "page:", {"cursor": ""}) and options.stage in (None, "list"):
        done = q.get_meta("list_done")
        if done and (options.end is None or options.end >= date.fromisoformat(done)):
            log("list: window still open, listing again from page 1")
            q.set_meta("list_done", "")  # an interrupted relist resumes instead of restarting
            q.set_meta("seen_cursors", "{}")
            q.requeue("list", ["page:"])
    if options.stage not in (None, "extract"):
        return
    done = q.results("extract")
    if options.vision:
        # Every --vision run retries image rows Codex hasn't read yet (first --vision, Codex errors).
        q.requeue("extract", [k for k, r in done
                              if (r or {}).get("preview_kind") == "image" and (r or {}).get("method") != "codex"])
    q.set_meta("options", json.dumps({"vision": options.vision}))
    # Codex-limit deferrals are re-extracted on every run until Codex reads them.
    q.requeue("extract", [k for k, r in done if (r or {}).get("vision_pending")])


def _join(v) -> str:
    return " | ".join(v) if isinstance(v, list) else (v or "")


def _lines(text: str | None) -> str:
    """Multi-line text on one line, lines joined with ` | `, so every CSV row is one line in any viewer."""
    return _join([ln.strip() for ln in (text or "").splitlines() if ln.strip()])


def _one_line(v: object) -> object:
    return " ".join(v.split()) if isinstance(v, str) and ("\n" in v or "\r" in v) else v


def _blank(v) -> object:
    return "" if v is None else v


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


def infer_accounts(rows: list[dict]) -> None:
    """Fill `ads_account_id` and `ads_account_source` in place. Only content.js Previews expose an Ads account;
    a row without one gets the account of its website when every exposed row of that website agrees."""
    by_site: dict[str, set[str]] = {}
    for r in rows:
        if r.get("ads_account_id") and r.get("website"):
            by_site.setdefault(r["website"], set()).add(r["ads_account_id"])
    for r in rows:
        if r.get("ads_account_id"):
            r["ads_account_source"] = "exposed"
            continue
        accounts = by_site.get(r.get("website") or "", set())
        if len(accounts) == 1:
            r["ads_account_id"], r["ads_account_source"] = next(iter(accounts)), "inferred"
        else:
            r["ads_account_id"], r["ads_account_source"] = "", ""


def _rows(q: WorkQueue, options: ScrapeOptions) -> list[dict]:
    region = _region_label(options.region)
    listing: dict[str, dict] = {}
    if options.variations:
        # The list stage refreshes detail payloads; fetch payloads keep the first listing.
        listing = {i.key: {k: v for k, v in i.payload.items() if k != "preview"} for i in q.items("detail")}
    extracts = {i.key: i for i in q.items("extract")}
    rows = []
    for f in q.items("fetch"):
        pl = {**f.payload, **listing.get(f.payload["creative_id"], {})}
        p = pl.get("preview") or {}
        item = extracts.get(f.key)
        c = (item.result if item and item.status == "done" else None) or {}
        status = copy_status(item.status if item else None, {**c, "format": pl.get("format", "")})
        account, ad_group = preview_ids(p.get("url", "")) if p.get("kind") == "content_js" else ("", "")
        cr = pl["creative_id"]
        conf = c.get("confidence")
        rows.append({
            "transparency_url": transparency_url(options.advertiser_id, cr, region),
            "creative_id": cr,
            "variation": pl["variation"],
            "format": pl.get("format", ""),
            "region": region,
            "first_shown": pl.get("first_shown", ""),
            "last_shown": pl.get("last_shown", ""),
            "days_shown": _blank(pl.get("days_shown")),
            **{k: c.get(k, "") for k in ("website", "landing_url", "display_url", "brand", "headline",
                                         "description", "cta")},
            "sitelinks": _join(c.get("sitelinks")),
            "full_text": _lines(c.get("full_text")),
            "video_id": c.get("video_id", ""),
            "video_url": watch_url(c["video_id"]) if c.get("video_id") else "",
            "video_title": c.get("video_title", ""),
            "preview_url": p.get("url", ""),
            "image_file": (f.result or {}).get("image_file", ""),
            "image_width": _blank(p.get("width")),
            "image_height": _blank(p.get("height")),
            "logo_url": c.get("logo_url", ""),
            "image_urls": _join(c.get("image_urls")),
            "extraction_method": c.get("method", ""),
            "extraction_model": c.get("model", ""),
            "extraction_confidence": "" if conf is None else round(float(conf), 3),
            "copy_status": status,
            "sample_weight": "" if status in ("not_sampled", "pending") else 1.0,  # Task 7 replaces the weight
            "ads_account_id": account,
            "ads_account_source": "",
            "ad_group_id": ad_group,
        })
    rows = [{k: _one_line(v) for k, v in r.items()} for r in rows]
    infer_accounts(rows)
    rows.sort(key=lambda r: (r["creative_id"], int(r["variation"])))
    return rows


_STAGE_LABELS = {"list": ("list", "page"), "detail": ("detail", "creative"), "fetch": ("fetch", "ad"),
                 "script": ("scripts", "script"), "extract": ("extract", "ad")}


def _progress_bars(q: WorkQueue, stage_names: list[str]) -> list[Bar]:
    """A creatives bar (Google's estimated total until the listing finishes, then the exact count) and one bar per
    running stage."""
    def listed(c: dict) -> int:
        return sum(n for (s, _), n in c.items() if s == "fetch")

    def list_finished() -> bool:
        return bool(q.get_meta("list_done"))

    def creatives(c: dict) -> tuple[int, int | None]:
        done = c.get(("extract", "done"), 0) + c.get(("extract", "failed"), 0)
        failed_before = sum(c.get((s, "failed"), 0) for s in ("fetch", "script"))
        if list_finished():
            return done + failed_before, listed(c)
        estimate = int(q.get_meta("count_estimate") or 0)
        return done + failed_before, max(estimate, listed(c)) or None

    bars = [Bar("creatives", creatives, "ad")]
    for name in stage_names:
        label, unit = _STAGE_LABELS.get(name, (name, "it"))
        total = (lambda c, n=name: sum(k for (s, _), k in c.items() if s == n) if list_finished() else None) \
            if name == "list" else None
        bars.append(stage_bar(name, label, unit, total))
    return bars


def scrape(options: ScrapeOptions, *, session: PacedSession | None = None, ocr=recognize,
           codex: CodexRunner | None = None, log: Callable[[str], None] = print_to_stderr,
           poll: float = 0.2) -> ScrapeResult:
    """Run (or resume) one scrape and export its CSV. Raises on setup errors; reports run outcomes."""
    query = _query(options.advertiser_id, options.region, options.start, options.end)
    progress: QueueProgress | None = None

    def emit(message: str) -> None:
        """`log`, or above the progress bars while they show."""
        (progress.write if progress is not None else log)(message)

    own_session = session is None
    if own_session:
        session = make_session(options.out, options.proxies, options.wait_on_block, emit)
    # oEmbed and the OCR domain check are optional: they must never wait out a block or back off,
    # whatever --wait-on-block says.
    oembed = session.derive(wait_on_block=False, max_retries=0)
    cache: ExtractionCache | None = None
    q: WorkQueue | None = None
    try:
        client = TransparencyClient(session)
        name = _advertiser_name(client, query, options.out)
        ent = entity_dir(options.out, TOOL_NAME, options.advertiser_id, name)
        cache = ExtractionCache(cache_dir(options.out) / "extract.sqlite3")
        if codex is None:
            codex = CodexRunner(options.vision_model, options.vision_limit)
        run = ent / "runs" / run_dir_name(options)
        run.mkdir(parents=True, exist_ok=True)
        q = WorkQueue(run / "state.sqlite3")
        _remove_done_content(q, run)
        _prepare_queue(q, options, emit)

        ctx = StageContext(client=client, session=session, query=query, entity=ent, run=run, cache=cache,
                           codex=codex, variations=options.variations, vision=options.vision, ocr=ocr, log=emit,
                           oembed=oembed, host_exists=HostChecker(oembed, cache, log=emit))
        ads = AdsStages(ctx)
        if options.stage in (None, "extract"):
            ads.retry_titles(q)  # transient oEmbed failures of earlier runs
        stages = ads.stages(options.stage)
        if options.progress and stages:
            progress = QueueProgress(_progress_bars(q, [s.name for s in stages])).start(q)
        try:
            rr = Pipeline(q, stages, poll=poll, log=emit).run() if stages else RunResult()
        finally:
            if progress is not None:
                progress.close()
                progress = None

        csv_path = run / "ads.csv"
        n = write_csv_atomic(csv_path, CSV_FIELDS, _rows(q, options))
        failed = {s: k for s in STAGE_NAMES if (k := q.count(s, "failed"))}
        result = ScrapeResult(csv_path, n, rr.blocked.until if rr.blocked else None, failed, codex.skipped,
                              list(rr.errors))
        _log_summary(result, codex, emit)
        return result
    finally:
        if q is not None:
            q.close()
        if cache is not None:
            cache.close()
        oembed.close()
        if own_session:
            session.close()


def _log_summary(r: ScrapeResult, codex: CodexRunner, log: Callable[[str], None]) -> None:
    log(f"exported {r.rows} rows to {r.csv_path}")
    if r.vision_skipped:
        log(f"warning: vision limit ({codex.limit}) reached; {r.vision_skipped} image(s) kept their OCR copy "
            "and will be sent to Codex on the next run")
    if r.failed:
        log("failed items: " + ", ".join(f"{s}: {n}" for s, n in r.failed.items()))
    for e in r.errors:
        log(f"error: {e}")
    if r.blocked_until is not None:
        log(f"blocked until {datetime.fromtimestamp(r.blocked_until):%H:%M}")
