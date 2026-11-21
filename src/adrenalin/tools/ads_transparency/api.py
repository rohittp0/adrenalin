"""Public API of the ads-transparency tool: `scrape()` one Advertiser × Region × Shown period to CSV."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.geo import region_to_geo_id
from adrenalin.common.http import AdaptivePace, Cooldown, HostChecker, PacedSession
from adrenalin.common.ocr import recognize
from adrenalin.common.ocr.codex import DEFAULT_VISION_MODEL, CodexRunner
from adrenalin.common.output import cache_dir, entity_dir, write_csv_atomic
from adrenalin.common.pipeline import Pipeline, RunResult, WorkQueue, print_to_stderr
from adrenalin.common.progress import Bar, QueueProgress, stage_bar
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.copy import (  # noqa: F401
    COPY_STATUSES,
    copy_status,
    is_broken_preview,
    stale_content_read,
)
from adrenalin.tools.ads_transparency.rpc import (
    HOST,
    ListedCreative,
    Query,
    TransparencyClient,
    preview_ids,
    transparency_url,
)
from adrenalin.tools.ads_transparency.sample_queue import (
    HELD,
    SampleTally,
    exhausted_reads,
    listing_stage,
    read_results,
    requeue_keeping_result,
    row_status,
    sample_groups,
    sampling_meta,
    seed_round,
)
from adrenalin.tools.ads_transparency.stages import (
    CONTENT_DIR,
    REREAD,
    REREAD_GIVEN_UP,
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
# simgad images: never blocked so far. Paced adaptively: from one a second, faster while responses stay clean,
# slower on errors (see `AdaptivePace`).
IMAGE_HOST, IMAGE_PACE = "tpc.googlesyndication.com", AdaptivePace(1.0, 0.2, 4.0)

CSV_FIELDS: tuple[str, ...] = (
    "transparency_url", "creative_id", "variation", "format", "region", "first_shown", "last_shown",
    "days_shown", "website", "landing_url", "display_url", "brand", "headline", "description", "cta",
    "sitelinks", "full_text", "video_id", "video_url", "video_title", "preview_url", "image_file",
    "image_width", "image_height", "logo_url", "image_urls", "extraction_method", "extraction_model",
    "extraction_confidence", "copy_status", "sample_weight", "ads_account_id", "ads_account_source", "ad_group_id",
)


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
    # Copy sample (ADR 0003): a large Inventory is read until Coverage reaches `coverage`; `read_all` reads every
    # Creative; `max_ads` caps the Creatives read at about N, head start included, and samples even a small Inventory
    # (plus up to 3 per launch-week x running/stopped x Ads account group, which can exceed N by far; without
    # Google's estimate every Creative is admitted). `p` is kept from a run folder's first run.
    read_all: bool = False
    coverage: float = 0.95
    max_ads: int | None = None

    def __post_init__(self) -> None:
        if not 0 < self.coverage < 1:
            raise ValueError(f"coverage must be between 0 and 1 (exclusive), not {self.coverage}")
        if self.max_ads is not None and self.max_ads < 1:
            raise ValueError(f"max_ads must be at least 1, not {self.max_ads}")


@dataclass
class ScrapeResult:
    csv_path: Path
    rows: int
    blocked_until: float | None
    failed: dict[str, int]
    vision_skipped: int
    # pipeline-level crashes (not item failures); a non-empty list is a failed run
    errors: list[str] = field(default_factory=list)
    # False: the listing did not finish (Incomplete run), so the CSV covers only the Creatives listed so far
    listing_complete: bool = True
    listed: int = 0
    estimate: int | None = None  # Google's rough count of Creatives
    coverage: float | None = None
    summary_path: Path | None = None


def make_session(out: Path, proxies: tuple[str, ...] = (), wait_on_block: bool = False,
                 log: Callable[[str], None] | None = print_to_stderr) -> PacedSession:
    return PacedSession(Cooldown(cache_dir(out) / "cooldown.sqlite3"),
                        {HOST: RPC_INTERVAL, SCRIPT_HOST: SCRIPT_INTERVAL},
                        proxies=proxies, wait_on_block=wait_on_block, log=log,
                        adaptive={IMAGE_HOST: IMAGE_PACE}, playwright_hosts=(HOST,))


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


def _prepare_sampling(q: WorkQueue, options: ScrapeOptions, log: Callable[[str], None]) -> None:
    """Refresh the Coverage target and cap from this run's options (`p` is kept; `--stage export` keeps them too,
    since it reads nothing), and on a full run queue a sampling round if held Creatives remain from an earlier,
    finished listing (e.g. a rerun with a higher `--coverage`, or `--all`). The round itself decides whether to
    admit more."""
    if options.stage == "export":
        return
    meta = sampling_meta(q)
    meta.update(target=options.coverage, max_ads=options.max_ads)
    q.set_meta("sampling", json.dumps(meta))
    estimate = meta.get("estimate", int(q.get_meta("count_estimate") or 0) or None)
    if "p" in meta and options.max_ads is not None and not options.read_all and estimate:
        would = sampling.admit_probability(estimate, max_ads=options.max_ads)
        if not math.isclose(would, meta["p"]):
            log(f"note: the head start was set by this run folder's first run (p={meta['p']:.4g}); --max-ads "
                f"{options.max_ads:,} only caps the reads that follow it")
    if options.stage is None and q.get_meta("list_done") and q.count(HELD, "pending"):
        seed_round(q)


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


def _confirmed_broken(r: dict | None) -> bool:
    """A Broken Preview whose other Variations were checked: permanent, never worth another Codex or RPC call."""
    return bool((r or {}).get("preview_broken") and not (r or {}).get("broken_unverified"))


def _reread_stale_content(q: WorkQueue, run: Path, *, extract: bool, script: bool) -> None:
    """Read again the content.js reads an earlier parser got wrong (`stale_content_read`), keeping each row's
    result until the new read is done. A script still on disk (unparsed ones are kept) is extracted again when
    `extract` runs; a deleted one is downloaded again by the script stage, which owns that host and its blocks,
    when `script` runs. Extract never downloads it: a block there would stop every stage (ADR 0001)."""
    scripts = {it.key: it for it in q.items("script")}
    on_disk = []
    for it in q.items("extract", "done"):
        if not stale_content_read(it.result or {}):
            continue
        rel = it.payload.get("content_file")
        if rel and (run / rel).is_file():
            on_disk.append(it.key)
            continue
        s = scripts.get(it.key)
        if not script or (s is not None and (s.status != "done" or (s.result or {}).get(REREAD_GIVEN_UP))):
            continue  # a download already queued (it keeps its attempts), or one given up
        q.put("script", it.key, {**it.payload, REREAD: True}, update_payload=True)
        q.requeue("script", [it.key])
    if extract:
        requeue_keeping_result(q, on_disk)


def _prepare_queue(q: WorkQueue, options: ScrapeOptions, run: Path, log: Callable[[str], None]) -> None:
    # Requeues only when the stage that redoes the work runs: a requeue clears the item's result,
    # so e.g. `--stage export` must not blank copy it cannot re-extract.
    if not q.put("list", "page:", {"cursor": ""}) and options.stage in (None, "list"):
        done = q.get_meta("list_done")
        if done and (options.end is None or options.end >= date.fromisoformat(done)):
            log("list: window still open, listing again from page 1")
            q.set_meta("list_done", "")  # an interrupted relist resumes instead of restarting
            q.set_meta("seen_cursors", "{}")
            q.requeue("list", ["page:"])
    _reread_stale_content(q, run, extract=options.stage in (None, "extract"), script=options.stage in (None, "fetch"))
    if options.stage not in (None, "extract"):
        return
    done = q.results("extract")
    # Before any requeue, which clears results: the count of runs whose detail response for a Broken Preview was
    # unparsable moves to the payload.
    if checks := {k: r["unparsable_checks"] for k, r in done
                  if (r or {}).get("broken_unverified") and r.get("unparsable_checks")}:
        for it in q.items("extract", "done"):
            if it.key in checks:
                q.put("extract", it.key, {**it.payload, "unparsable_checks": checks[it.key]}, update_payload=True)
    if options.vision:
        # Every --vision run retries image rows Codex hasn't read yet (first --vision, Codex errors).
        q.requeue("extract", [k for k, r in done
                              if (r or {}).get("preview_kind") == "image" and (r or {}).get("method") != "codex"
                              and not _confirmed_broken(r)])
    q.set_meta("options", json.dumps({"vision": options.vision}))
    # Codex-limit deferrals are re-extracted on every run until Codex reads them.
    q.requeue("extract", [k for k, r in done if (r or {}).get("vision_pending")])
    # Rows read before Broken Previews were detected still hold the error page as copy; and a Broken Preview whose
    # other Variations could not be checked (block, network) is checked again.
    q.requeue("extract", [k for k, r in done if (r or {}).get("preview_kind") == "image"
                          and ((r or {}).get("broken_unverified")
                               or is_broken_preview(f"{r.get('headline') or ''} {r.get('full_text') or ''}"))])


def _join(v) -> str:
    return " | ".join(v) if isinstance(v, list) else (v or "")


def _lines(text: str | None) -> str:
    """Multi-line text on one line, lines joined with ` | `, so every CSV row is one line in any viewer."""
    return _join([ln.strip() for ln in (text or "").splitlines() if ln.strip()])


def _one_line(v: object) -> object:
    return " ".join(v.split()) if isinstance(v, str) and ("\n" in v or "\r" in v) else v


def _blank(v) -> object:
    return "" if v is None else v


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


def is_sampled(q: WorkQueue) -> bool:
    """Whether the run reads a Copy sample (admission probability below 1) rather than the whole Inventory."""
    return sampling_meta(q).get("p", 1.0) < 1.0


def _rows(q: WorkQueue, options: ScrapeOptions, tally: SampleTally, sampled: bool) -> list[dict]:
    """CSV rows: every Variation that reached fetch, one Variation-1 row per Creative still in (or failed in) detail,
    and one `not_sampled` row per held Creative. Every row of a Creative carries the Creative's weight
    (`SampleTally.weight`, decided by its Variation 1)."""
    region = _region_label(options.region)
    listing: dict[str, dict] = {}
    fetched = q.items("fetch")
    # (row key, payload, fetch result, held)
    entries = [(i.key, i.payload, i.result, False) for i in fetched]
    if options.variations:
        details = q.items("detail")
        # The list stage refreshes detail payloads; fetch payloads keep the first listing.
        listing = {i.key: {k: v for k, v in i.payload.items() if k != "preview"} for i in details}
        have_rows = {i.payload["creative_id"] for i in fetched}
        entries += [(f"{i.key}#1", i.payload, None, False) for i in details if i.key not in have_rows]
    entries += [(i.key if "#" in i.key else f"{i.key}#1", i.payload, None, True) for i in q.items(HELD)]
    extracts = q.items("extract")
    statuses = {i.key: i.status for i in extracts}
    copies = read_results(extracts)
    exhausted = exhausted_reads(q)
    rows = []
    for key, payload, fetch_result, is_held in entries:
        pl = {**payload, **listing.get(payload["creative_id"], {})}
        pl.setdefault("variation", 1)  # a held (or not yet detailed) Creative's Variations aren't listed yet
        p = pl.get("preview") or {}
        extract_status = None if is_held else "done" if key in copies else statuses.get(key)
        c = (None if is_held else copies.get(key)) or {}
        status = row_status(extract_status, c, pl.get("format", ""), key, exhausted, held=is_held)
        weight = "" if is_held else tally.weight(pl["creative_id"], sampled)
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
            "preview_url": c.get("preview_url") or p.get("url", ""),  # the Variation actually read
            "image_file": c.get("image_file") or (fetch_result or {}).get("image_file", ""),
            "image_width": _blank(p.get("width")),
            "image_height": _blank(p.get("height")),
            "logo_url": c.get("logo_url", ""),
            "image_urls": _join(c.get("image_urls")),
            "extraction_method": c.get("method", ""),
            "extraction_model": c.get("model", ""),
            "extraction_confidence": "" if conf is None else round(float(conf), 3),
            "copy_status": status,
            "sample_weight": weight,
            "ads_account_id": account,
            "ads_account_source": "",
            "ad_group_id": ad_group,
        })
    rows = [{k: _one_line(v) for k, v in r.items()} for r in rows]
    infer_accounts(rows)
    rows.sort(key=lambda r: (r["creative_id"], int(r["variation"])))
    return rows


_STAGE_LABELS = {"list": ("list", "page"), "sample": ("sample", "round"), "detail": ("detail", "creative"),
                 "fetch": ("fetch", "ad"), "script": ("scripts", "script"), "extract": ("extract", "ad")}


def _progress_bars(q: WorkQueue, stage_names: list[str]) -> list[Bar]:
    """A creatives bar (Google's estimated total until the listing finishes, then the exact count) and one bar per
    running stage."""
    def listed(c: dict) -> int:
        if is_sampled(q):  # the listed Inventory: admitted Creatives (fetch, or detail with --variations) and held
            stage = listing_stage(any(s == "detail" for s, _ in c))
            return sum(n for (s, _), n in c.items() if s in (stage, HELD))
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


def _previous_stage_seconds(path: Path) -> dict:
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    seconds = old.get("stage_seconds") if isinstance(old, dict) else None
    return seconds if isinstance(seconds, dict) else {}


def build_summary(rows: list[dict], q: WorkQueue, rr: RunResult, tally: SampleTally, sampled: bool) -> dict:
    """The run's summary.json content. Coverage, sampling and groups are filled for a Copy sample and stay empty
    for a full read. Counts are per Creative (Variation 1), so with `--variations` the `sample_weight` of Variation
    1 rows sums to each bucket's Inventory. `listed` counts the listed Creatives: admitted and held."""
    groups = tally.groups if sampled else None
    accounts: dict[tuple[str, str], dict] = {}
    for r in rows:
        if not r.get("ads_account_id"):
            continue
        a = accounts.setdefault((r["ads_account_id"], r["ads_account_source"]), {
            "ads_account_id": r["ads_account_id"], "source": r["ads_account_source"],
            "websites": Counter(), "formats": Counter(), "rows": 0})
        a["rows"] += 1
        a["websites"][r.get("website") or ""] += 1
        a["formats"][r.get("format") or ""] += 1
    ranked = sorted(accounts.values(), key=lambda a: -a["rows"])
    estimate = int(q.get_meta("count_estimate") or 0) or None
    return {
        "listing_complete": bool(q.get_meta("list_done")),
        "listed": len(tally.group_of),
        "google_estimate": estimate,
        "copy_status": dict(Counter(r["copy_status"] for r in rows)),
        "coverage": None if groups is None else sampling.coverage([m for g in groups.values() for m in g.messages]),
        "sampling": None if groups is None else {
            **sampling_meta(q),
            "inventory": sum(g.inventory for g in groups.values()),
            "processed": sum(g.processed for g in groups.values()),
        },
        # per sampling group (launch period x running/stopped bucket x Ads account); `processed`: copy reading
        # finished (read, no copy, broken or unreadable)
        "groups": [] if groups is None else [
            {"bucket": k, "inventory": g.inventory, "processed": g.processed,
             "messages": len({m for m in g.messages if m}), "coverage": sampling.coverage(g.messages)}
            for k, g in sorted(groups.items())],
        "ads_accounts": [{"ads_account_id": a["ads_account_id"], "source": a["source"],
                          "websites": dict(a["websites"]), "formats": dict(a["formats"])} for a in ranked],
        "stage_seconds": {s: {k: round(v, 1) for k, v in t.items()} for s, t in rr.stage_seconds.items()},
    }


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
        _prepare_queue(q, options, run, emit)
        _prepare_sampling(q, options, emit)

        ctx = StageContext(client=client, session=session, query=query, entity=ent, run=run, cache=cache,
                           codex=codex, variations=options.variations, vision=options.vision, ocr=ocr, log=emit,
                           oembed=oembed, host_exists=HostChecker(oembed, cache, log=emit),
                           read_all=options.read_all, coverage=options.coverage, max_ads=options.max_ads)
        ads = AdsStages(ctx)
        if options.stage in (None, "extract"):
            ads.retry_titles(q)  # transient oEmbed failures of earlier runs
        stages = ads.stages(q, options.stage)
        if options.progress and stages:
            progress = QueueProgress(_progress_bars(q, [s.name for s in stages])).start(q)
        try:
            rr = Pipeline(q, stages, poll=poll, log=emit).run() if stages else RunResult()
        finally:
            if progress is not None:
                progress.close()
                progress = None

        csv_path = run / "ads.csv"
        tally = sample_groups(q, options.variations, query, ctx.today())
        sampled = is_sampled(q)
        rows = _rows(q, options, tally, sampled)
        n = write_csv_atomic(csv_path, CSV_FIELDS, rows)
        summary_path = run / "summary.json"
        if not stages:  # nothing ran (`--stage export`): keep the stage times of the run that did the work
            rr.stage_seconds = _previous_stage_seconds(summary_path)
        summary = build_summary(rows, q, rr, tally, sampled)
        _write_json_atomic(summary_path, summary)
        failed = {s: k for s in STAGE_NAMES if (k := q.count(s, "failed"))}
        result = ScrapeResult(csv_path, n, rr.blocked.until if rr.blocked else None, failed, codex.skipped,
                              list(rr.errors), summary["listing_complete"], summary["listed"],
                              summary["google_estimate"], summary["coverage"], summary_path)
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
        log(blocked_message(r.blocked_until))
    if r.coverage is not None:
        log(f"Coverage {r.coverage:.3f} of the Copy sample's Messages")
    if not r.listing_complete:
        log(incomplete_message(r))


def blocked_message(until: float) -> str:
    return f"blocked until {datetime.fromtimestamp(until):%H:%M}"


def incomplete_message(r: ScrapeResult) -> str:
    of = f" of ~{r.estimate:,}" if r.estimate else ""
    return f"INCOMPLETE: listed {r.listed:,}{of} Creatives; rerun the same command to continue"


def is_outcome_line(message: str) -> bool:
    """Whether `message` is one of the run-outcome lines `scrape` logs last (blocked until, INCOMPLETE). The CLI
    prints those itself, from the `ScrapeResult`, so it leaves them out of the log."""
    return message.startswith("INCOMPLETE: ") or re.fullmatch(r"blocked until \d\d:\d\d", message) is not None
