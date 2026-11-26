"""Public API of the meta-ad-library tool: `scrape()` one Facebook Page x Region x Shown period to CSV."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.http import PacedSession, run_session
from adrenalin.common.ocr import recognize
from adrenalin.common.output import cache_dir, entity_dir, write_csv_atomic, write_json_atomic
from adrenalin.common.pipeline import Pipeline, RunResult, WorkQueue
from adrenalin.common.progress import run_progress, stage_bar
from adrenalin.common.runlog import run_log
from adrenalin.tools.meta_ad_library.client import MetaClient, MetaQuery, country_code
from adrenalin.tools.meta_ad_library.hosts import HOSTS
from adrenalin.tools.meta_ad_library.parse import DocNotFound
from adrenalin.tools.meta_ad_library.stages import (
    FATAL,
    STAGE_NAMES,
    MetaContext,
    MetaStages,
    media_urls,
    row_from_payload,
    row_items,
)

TOOL_NAME = "meta-ad-library"
log = logging.getLogger(__name__)

CSV_FIELDS = [
    "library_id", "ad_library_url", "page_id", "page_name", "variation", "display_format", "is_active",
    "start_date", "end_date", "platforms", "collation_id", "collation_count", "body", "title", "link_description",
    "caption", "cta_text", "link_url", "card_titles", "card_bodies", "card_links", "image_urls", "image_files",
    "video_urls", "image_text", "eu_total_reach", "eu_reach_by_country", "uk_total_reach", "payer", "beneficiary",
]

EU_COUNTRIES = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
DOC_NOT_FOUND = ("Meta changed its Ad Library queries (GraphQL document not found); "
                 "update SEARCH_DOC_ID / DETAIL_DOC_ID in tools/meta_ad_library/client.py")
INACTIVE_NOTE = ("Inactive Creatives outside the EU are only partly listed by Meta; "
                 "don't read stopped counts as complete.")
_PAGE_ARG_ERROR = "expected a numeric Page ID or an Ad Library URL containing view_all_page_id=…"
_VIEW_ALL = re.compile(r"[?&]view_all_page_id=(\d+)(?:&|#|$)")


@dataclass(frozen=True)
class MetaOptions:
    page_id: str
    region: str = "anywhere"
    status: str = "all"  # all | active | inactive
    start: date | None = None
    end: date | None = None
    variations: bool = False  # one row per Variation (DCO Creatives), else Variation 1 only
    eu_reach: bool = False  # one detail request per Creative
    ocr: bool = False  # read the text in the downloaded media files
    wait_on_block: bool = False
    solve_captcha: bool = False
    stage: str | None = None  # list | fetch | export
    out: Path = Path("out")
    proxies: tuple[str, ...] = ()
    progress: bool = False  # tqdm progress bars on stderr


@dataclass
class MetaResult:
    csv_path: Path
    rows: int
    blocked_until: float | None
    failed: dict[str, int]
    errors: list[str] = field(default_factory=list)
    # False: the listing did not finish (Incomplete run), so the CSV covers only the Creatives listed so far
    listing_complete: bool = True
    listed: int = 0
    summary_path: Path | None = None


def parse_page_arg(text: str) -> str:
    """A Page ID from digits or from any URL with `view_all_page_id=<digits>`."""
    text = text.strip()
    if text.isascii() and text.isdigit():
        return text
    if m := _VIEW_ALL.search(text):
        return m.group(1)
    raise ValueError(_PAGE_ARG_ERROR)


def make_session(out: Path, proxies: tuple[str, ...] = (), wait_on_block: bool = False, *,
                 solve_captcha: bool = False) -> PacedSession:
    return run_session(out, HOSTS, proxies=proxies, wait_on_block=wait_on_block, solve_captcha=solve_captcha)


def _query(options: MetaOptions) -> MetaQuery:
    if options.start and options.end and options.start > options.end:
        raise ValueError(f"start {options.start} is after end {options.end}")
    if options.status not in ("all", "active", "inactive"):
        raise ValueError(f"status must be all, active or inactive, not {options.status!r}")
    return MetaQuery(options.page_id, country_code(options.region), options.status, options.start, options.end)


def _page_name(client: MetaClient, query: MetaQuery, out: Path) -> str:
    """The Page's name for the entity folder: cached in `meta_pages.json`, else from the first listed Creative
    (the Page's ID when it has none, or no name)."""
    path = cache_dir(out) / "meta_pages.json"
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    name = known.get(query.page_id)
    if isinstance(name, str) and name:
        return name
    try:
        page = client.search(query, None)
    except DocNotFound as e:
        raise ValueError(DOC_NOT_FOUND) from e
    name = next((c.page_name for c in page.creatives if c.page_name), "")
    if not name:
        return query.page_id
    known[query.page_id] = name
    write_json_atomic(path, known)
    return name


def run_dir_name(options: MetaOptions) -> str:
    region = "anywhere" if options.region.lower() == "anywhere" else options.region.upper()
    name = f"{region}_{options.status}_{options.start or 'all'}_{options.end or 'all'}"
    return name + ("_variations" if options.variations else "")


def _join(values) -> str:
    return " | ".join(values)


def _blank(v) -> object:
    return "" if v is None else v


def _rows(q: WorkQueue, options: MetaOptions) -> list[dict]:
    texts = {key: (r or {}).get("image_text", "") for key, r in q.results("ocr")} if options.ocr else {}
    rows: dict[str, dict] = {}
    for item in row_items(q):
        if item.key in rows:
            continue
        c, v, n = row_from_payload(item.payload)
        eu = item.payload.get("eu_reach") or {}
        cards = v.cards
        rows[item.key] = {
            "library_id": c.library_id,
            "ad_library_url": f"https://www.facebook.com/ads/library/?id={c.library_id}",
            "page_id": c.page_id, "page_name": c.page_name, "variation": n, "display_format": c.display_format,
            "is_active": str(c.is_active).lower(), "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat() if c.end_date else "", "platforms": _join(c.platforms),
            "collation_id": _blank(c.collation_id), "collation_count": _blank(c.collation_count),
            "body": v.body, "title": v.title, "link_description": v.link_description, "caption": v.caption,
            "cta_text": v.cta_text, "link_url": v.link_url,
            "card_titles": _join(k.title for k in cards), "card_bodies": _join(k.body for k in cards),
            "card_links": _join(k.link_url for k in cards),
            "image_urls": _join(media_urls(v)),
            "image_files": _join(((item.result or {}).get("image_files")) or []),
            "video_urls": _join([*v.video_urls, *(k.video_url for k in cards if k.video_url)]),
            "image_text": texts.get(item.key, ""),
            "eu_total_reach": _blank(eu.get("eu_total")),
            "eu_reach_by_country": json.dumps(eu["eu_by_country"], separators=(",", ":"), sort_keys=True)
            if eu.get("eu_by_country") else "",
            "uk_total_reach": _blank(eu.get("uk_total")),
            "payer": eu.get("payer", ""), "beneficiary": eu.get("beneficiary", ""),
        }
    return sorted(rows.values(), key=lambda r: (r["library_id"], r["variation"]))


def _listing_complete(q: WorkQueue) -> bool:
    return bool(q.get_meta("list_done")) and not any(
        it.status in ("pending", "running", "failed") for it in q.items("list"))


def _previous_stage_seconds(path: Path) -> dict:
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    seconds = old.get("stage_seconds") if isinstance(old, dict) else None
    return seconds if isinstance(seconds, dict) else {}


_STAGE_LABELS = {"list": ("list", "page"), "detail": ("detail", "creative"), "fetch": ("fetch", "ad"),
                 "ocr": ("ocr", "ad")}


def scrape(options: MetaOptions, *, session: PacedSession | None = None, ocr=recognize,
           poll: float = 0.2) -> MetaResult:
    """Run (or resume) one scrape and export its CSV. Raises on setup errors; reports run outcomes.
    Messages go to the `adrenalin` logger (and `run.log` in the run folder)."""
    query = _query(options)
    own_session = session is None
    if own_session:
        session = make_session(options.out, options.proxies, options.wait_on_block,
                               solve_captcha=options.solve_captcha)
    cache: ExtractionCache | None = None
    q: WorkQueue | None = None
    try:
        client = MetaClient(session)
        ent = entity_dir(options.out, TOOL_NAME, options.page_id, _page_name(client, query, options.out))
        cache = ExtractionCache(cache_dir(options.out) / "extract.sqlite3")
        run = ent / "runs" / run_dir_name(options)
        run.mkdir(parents=True, exist_ok=True)
        with run_log(run):
            q = WorkQueue(run / "state.sqlite3")
            ctx = MetaContext(client=client, session=session, query=query, entity=ent, cache=cache,
                              variations=options.variations, eu_reach=options.eu_reach, ocr=options.ocr,
                              recognize=ocr)
            return _scrape_run(options, q, MetaStages(ctx), run=run, poll=poll)
    finally:
        if q is not None:
            q.close()
        if cache is not None:
            cache.close()
        if own_session:
            session.close()


def _scrape_run(options: MetaOptions, q: WorkQueue, stages_obj: MetaStages, *, run: Path,
                poll: float) -> MetaResult:
    if options.stage != "export":
        stages_obj.prepare(q, options.stage)
    stages = stages_obj.stages(q, options.stage)
    bars = [stage_bar(s.name, *_STAGE_LABELS[s.name]) for s in stages]
    with run_progress(q, bars, options.progress and bool(stages)):
        rr = Pipeline(q, stages, poll=poll, fatal=FATAL).run() if stages else RunResult()

    rows = _rows(q, options)
    csv_path = run / "ads.csv"
    n = write_csv_atomic(csv_path, CSV_FIELDS, rows)
    summary_path = run / "summary.json"
    if not stages:  # nothing ran (`--stage export`): keep the stage times of the run that did the work
        rr.stage_seconds = _previous_stage_seconds(summary_path)
    listed = {r["library_id"] for r in rows}
    complete = _listing_complete(q)
    write_json_atomic(summary_path, {
        "listed": len(listed), "listing_complete": complete, "rows": n,
        "inactive_listed": len({r["library_id"] for r in rows if r["is_active"] == "false"}),
        "stage_seconds": {s: {k: round(v, 1) for k, v in t.items()} for s, t in rr.stage_seconds.items()},
    })
    failed = {s: k for s in STAGE_NAMES if (k := q.count(s, "failed"))}
    result = MetaResult(csv_path, n, rr.blocked.until if rr.blocked else None, failed, list(rr.errors), complete,
                        len(listed), summary_path)
    _log_summary(result, options)
    return result


def _log_summary(r: MetaResult, options: MetaOptions) -> None:
    """The run's closing lines. The outcome lines (blocked until, INCOMPLETE) are printed by the CLI from the
    `MetaResult`, and library callers read the result."""
    log.info("exported %d rows to %s", r.rows, r.csv_path)
    if r.failed:
        log.info("failed items: " + ", ".join(f"{s}: {n}" for s, n in r.failed.items()))
    doc_not_found = [e for e in r.errors if "DocNotFound" in e]
    if doc_not_found:
        log.error(DOC_NOT_FOUND)
    for e in r.errors:
        if e not in doc_not_found:
            log.error("%s", e)
    if options.region.lower() != "anywhere" and options.region.upper() not in EU_COUNTRIES:
        log.info(INACTIVE_NOTE)


def incomplete_message(r: MetaResult) -> str:
    return f"INCOMPLETE: listed {r.listed:,} Creatives; rerun the same command to continue"
