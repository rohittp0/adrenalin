"""Public API of the ads-transparency tool: `scrape()` one Advertiser × Region × Shown period to CSV."""
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.geo import region_label, region_to_geo_id
from adrenalin.common.http import Cooldown, HostChecker, PacedSession, run_session
from adrenalin.common.ocr import recognize
from adrenalin.common.ocr.codex import DEFAULT_VISION_MODEL, CodexRunner
from adrenalin.common.output import (
    cache_dir, cached_entity_name, entity_dir, previous_stage_seconds, write_csv_atomic, write_json_atomic)
from adrenalin.common.pipeline import Pipeline, RunResult, WorkQueue
from adrenalin.common.progress import Bar, run_progress, stage_bar
from adrenalin.common.runlog import run_log
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.countries import (
    COUNTRY_FIELDS, countries_message, countries_pending, country_rows, current_targets)  # noqa: F401  (countries_message: used by the CLI)
from adrenalin.tools.ads_transparency.copy import (  # noqa: F401
    COPY_STATUSES,
    copy_status,
    is_broken_preview,
    stale_content_read,
)
from adrenalin.tools.ads_transparency.hosts import DEFAULT, HOSTS
from adrenalin.tools.ads_transparency.rpc import (
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
log = logging.getLogger(__name__)

CSV_FIELDS: tuple[str, ...] = (
    "transparency_url", "creative_id", "variation", "format", "region", "first_shown", "last_shown",
    "days_shown", "website", "landing_url", "display_url", "brand", "headline", "description", "cta",
    "sitelinks", "full_text", "video_id", "video_url", "video_title", "preview_url", "image_file",
    "image_width", "image_height", "logo_url", "image_urls", "extraction_method", "extraction_model",
    "extraction_confidence", "copy_status", "sample_weight", "sample_role", "ads_account_id", "ads_account_source", "ad_group_id",
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
    solve_captcha: bool = False
    wait_on_block: bool = False
    stage: Literal["list", "fetch", "extract", "export"] | None = None
    out: Path = Path("out")
    proxies: tuple[str, ...] = ()
    progress: bool = False  # tqdm progress bars on stderr
    # Representative sampling uses Inventory-only strata and a saved random seed.
    # Novelty mode retains its legacy score/cap; --all bypasses sampling.
    sampling_mode: Literal["stratified", "novelty"] = "stratified"
    read_all: bool = False
    coverage: float = 0.95
    max_ads: int | None = None
    countries: bool = True  # Shown countries of the weighted Copy sample (`--no-countries` skips them)

    def __post_init__(self) -> None:
        if self.sampling_mode not in {"stratified", "novelty"}:
            raise ValueError("sampling_mode must be stratified or novelty")
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
    countries_pending: int = 0  # Copy-sample Creatives whose Shown countries are not fetched yet


def make_session(out: Path, proxies: tuple[str, ...] = (), wait_on_block: bool = False, *,
                 solve_captcha: bool = False) -> PacedSession:
    return run_session(out, HOSTS, default=DEFAULT, proxies=proxies, wait_on_block=wait_on_block,
                       solve_captcha=solve_captcha)


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


def _advertiser_name(client: TransparencyClient, query: Query, out: Path) -> str:
    name = cached_entity_name(out, "advertisers.json", query.advertiser_id, lambda: client.advertiser_name(query))
    if not name:
        raise ValueError(f"No creatives found for {query.advertiser_id}")
    return name


def _prepare_sampling(q: WorkQueue, options: ScrapeOptions) -> None:
    """Refresh the Coverage target and cap from this run's options (`p` is kept; `--stage export` keeps them too,
    since it reads nothing), and on a full run queue a sampling round if held Creatives remain from an earlier,
    finished listing (e.g. a rerun with a higher `--coverage`, or `--all`). The round itself decides whether to
    admit more."""
    if options.stage == "export":
        return
    meta = sampling_meta(q)
    if options.sampling_mode == "stratified" and not options.read_all:
        meta.update(mode="stratified", target=None, max_ads=options.max_ads, p=0.0)
        log.info(f"sample: representative target {options.max_ads or sampling.DEFAULT_SAMPLE_SIZE:,}, "
            "with at least 3 per group; prior reads outside the design remain exploratory.")
    else:
        if meta.get("mode") == "stratified":
            meta["p"] = 1.0 if options.read_all or not q.count(HELD, "pending") else 0.0
            meta.pop("cohort", None)
        meta.pop("mode", None)
        meta.update(target=options.coverage, max_ads=options.max_ads)
    q.set_meta("sampling", json.dumps(meta))
    estimate = meta.get("estimate", int(q.get_meta("count_estimate") or 0) or None)
    if options.sampling_mode == "novelty" and "p" in meta and options.max_ads is not None and not options.read_all and estimate:
        would = sampling.admit_probability(estimate, max_ads=options.max_ads)
        if not math.isclose(would, meta["p"]):
            log.info(f"note: the head start was set by this run folder's first run (p={meta['p']:.4g}); --max-ads "
                f"{options.max_ads:,} only caps the reads that follow it")
    if options.stage is None and q.get_meta("list_done") and (q.count(HELD, "pending") or options.sampling_mode == "stratified"):
        seed_round(q)


def run_dir_name(options: ScrapeOptions) -> str:
    name = f"{region_label(options.region)}_{options.start or 'all'}_{options.end or 'all'}"
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


def _prepare_queue(q: WorkQueue, options: ScrapeOptions, run: Path) -> None:
    # Requeues only when the stage that redoes the work runs: a requeue clears the item's result,
    # so e.g. `--stage export` must not blank copy it cannot re-extract.
    if not q.put("list", "page:", {"cursor": ""}) and options.stage in (None, "list"):
        done = q.get_meta("list_done")
        if done and (options.end is None or options.end >= date.fromisoformat(done)):
            log.info("list: window still open, listing again from page 1")
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
    region = region_label(options.region)
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
            "sample_role": ("representative" if cr in tally.representative else "exploratory"
                            if cr in tally.processed else "not_sampled") if tally.representative is not None
                           else "exploratory" if sampled else "census",
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
    """Separate listed Inventory from copy work currently admitted, plus stage bars."""
    def listed(c: dict) -> int:
        # Detail items represent Creatives when variations are requested; fetch
        # items then represent Variations and must not inflate the Inventory.
        stage = listing_stage("detail" in stage_names or any(s == "detail" for s, _ in c))
        return sum(n for (s, _), n in c.items() if s in (stage, HELD))

    def list_finished() -> bool:
        return bool(q.get_meta("list_done"))

    def inventory(c: dict) -> tuple[int, int | None]:
        n = listed(c)
        estimate = int(q.get_meta("count_estimate") or 0)
        return n, n if list_finished() else max(estimate, n) or None

    def copy_queued(c: dict) -> tuple[int, int | None]:
        done = c.get(("extract", "done"), 0) + c.get(("extract", "failed"), 0)
        failed_before = sum(c.get((s, "failed"), 0) for s in ("fetch", "script"))
        queued = sum(n for (s, _), n in c.items() if s == "fetch")
        return done + failed_before, queued

    # Copy's total grows as sampling admits more work; its ETA is only for the
    # current queue, never an estimate of time to reach Coverage.
    bars = [Bar("inventory", inventory, "ad"), Bar("creatives queued", copy_queued, "copy")]
    for name in stage_names:
        label, unit = _STAGE_LABELS.get(name, (name, "it"))
        total = (lambda c, n=name: sum(k for (s, _), k in c.items() if s == n) if list_finished() else None) \
            if name == "list" else None
        bars.append(stage_bar(name, label, unit, total))
    return bars


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
        "representative_sample": None if tally.representative is None else {
            "selected": len(tally.representative),
            "processed": len(tally.representative & tally.processed),
            "pending": len(tally.representative - tally.processed),
            "exploratory_processed": len(tally.processed - tally.representative),
            "weight_basis": "stratum Inventory / selected Creatives; not adjusted for unreadable copy",
        },
        "coverage": None if groups is None or tally.representative is not None else sampling.coverage([m for g in groups.values() for m in g.messages]),
        "sampling": None if groups is None else {
            **sampling_meta(q),
            "inventory": sum(g.inventory for g in groups.values()),
            "processed": sum(g.processed for g in groups.values()),
        },
        # per sampling group (launch period x running/stopped bucket x Ads account); `processed`: copy reading
        # finished (read, no copy, broken or unreadable)
        "groups": [] if groups is None else [
            {"bucket": k, "inventory": g.inventory, "processed": g.processed,
             "messages": len({m for m in g.messages if m}), "coverage": None if tally.representative is not None else sampling.coverage(g.messages)}
            for k, g in sorted(groups.items())],
        "ads_accounts": [{"ads_account_id": a["ads_account_id"], "source": a["source"],
                          "websites": dict(a["websites"]), "formats": dict(a["formats"])} for a in ranked],
        "stage_seconds": {s: {k: round(v, 1) for k, v in t.items()} for s, t in rr.stage_seconds.items()},
    }


def scrape(options: ScrapeOptions, *, session: PacedSession | None = None, ocr=recognize,
           codex: CodexRunner | None = None, poll: float = 0.2) -> ScrapeResult:
    """Run (or resume) one scrape and export its CSV. Raises on setup errors; reports run outcomes.
    Messages go to the `adrenalin` logger (and `run.log` in the run folder)."""
    query = _query(options.advertiser_id, options.region, options.start, options.end)
    own_session = session is None
    if own_session:
        session = make_session(options.out, options.proxies, options.wait_on_block,
                               solve_captcha=options.solve_captcha)
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
        with run_log(run):
            q = WorkQueue(run / "state.sqlite3")
            return _scrape_run(options, q, client=client, session=session, oembed=oembed, query=query, ent=ent,
                               run=run, cache=cache, ocr=ocr, codex=codex, poll=poll)
    finally:
        if q is not None:
            q.close()
        if cache is not None:
            cache.close()
        oembed.close()
        if own_session:
            session.close()


def _scrape_run(options: ScrapeOptions, q: WorkQueue, *, client: TransparencyClient, session: PacedSession,
                oembed: PacedSession, query: Query, ent: Path, run: Path, cache: ExtractionCache, ocr,
                codex: CodexRunner, poll: float) -> ScrapeResult:
    _remove_done_content(q, run)
    _prepare_queue(q, options, run)
    _prepare_sampling(q, options)

    ctx = StageContext(client=client, session=session, query=query, entity=ent, run=run, cache=cache,
                       codex=codex, variations=options.variations, vision=options.vision, ocr=ocr,
                       oembed=oembed, host_exists=HostChecker(oembed, cache),
                       read_all=options.read_all, coverage=options.coverage, max_ads=options.max_ads,
                       sampling_mode=options.sampling_mode, countries=options.countries)
    for _, result in q.results("extract"):
        if result.get("website"):
            ctx.host_exists.remember(result["website"])
    ads = AdsStages(ctx)
    if options.stage in (None, "list"):
        ads.queue_countries(q)  # targets chosen by earlier runs, and backfill of runs from before Shown countries
    if options.stage in (None, "extract"):
        ads.retry_titles(q)  # transient oEmbed failures of earlier runs
    stages = ads.stages(q, options.stage)
    with run_progress(q, _progress_bars(q, [s.name for s in stages]), options.progress and bool(stages)):
        rr = Pipeline(q, stages, poll=poll).run() if stages else RunResult()

    csv_path = run / "ads.csv"
    tally = sample_groups(q, options.variations, query, ctx.today())
    sampled = is_sampled(q)
    rows = _rows(q, options, tally, sampled)
    n = write_csv_atomic(csv_path, CSV_FIELDS, rows)
    summary_path = run / "summary.json"
    if not stages:  # nothing ran (`--stage export`): keep the stage times of the run that did the work
        rr.stage_seconds = previous_stage_seconds(summary_path)
    summary = build_summary(rows, q, rr, tally, sampled)
    write_json_atomic(summary_path, summary)
    pending_countries = 0
    if options.countries:
        targets = current_targets(q, tally, options.variations, options.max_ads)
        write_csv_atomic(run / "countries.csv", COUNTRY_FIELDS, country_rows(q, tally, sampled, targets))
        pending_countries = countries_pending(q, targets)
    failed = {s: k for s in STAGE_NAMES if (k := q.count(s, "failed"))}
    if summary["representative_sample"] is not None:
        representative = summary["representative_sample"]
        log.info(f"Representative sample: {representative['processed']:,}/{representative['selected']:,} processed; "
                 f"{representative['exploratory_processed']:,} additional exploratory reads retained")
    result = ScrapeResult(csv_path, n, rr.blocked.until if rr.blocked else None, failed, codex.skipped,
                          list(rr.errors), summary["listing_complete"], summary["listed"],
                          summary["google_estimate"], summary["coverage"], summary_path,
                          countries_pending=pending_countries)
    _log_summary(result, codex)
    return result


def _log_summary(r: ScrapeResult, codex: CodexRunner) -> None:
    """The run's closing lines. The outcome lines (blocked until, INCOMPLETE, Shown countries pending) are not
    logged: the CLI prints them from the `ScrapeResult`, and library callers read the result."""
    log.info(f"exported {r.rows} rows to {r.csv_path}")
    if r.vision_skipped:
        log.warning(f"vision limit ({codex.limit}) reached; {r.vision_skipped} image(s) kept their OCR copy "
                    "and will be sent to Codex on the next run")
    if r.failed:
        log.info("failed items: " + ", ".join(f"{s}: {n}" for s, n in r.failed.items()))
    for e in r.errors:
        log.error("%s", e)
    if r.coverage is not None:
        log.info(f"Novelty repetition score {r.coverage:.3f} (not population Coverage)")


def blocked_message(until: float) -> str:
    return f"blocked until {datetime.fromtimestamp(until):%H:%M}"


def incomplete_message(r: ScrapeResult) -> str:
    of = f" of ~{r.estimate:,}" if r.estimate else ""
    return f"INCOMPLETE: listed {r.listed:,}{of} Creatives; rerun the same command to continue"
