"""`adrenalin ads-transparency`: export an Advertiser's Google Ads Transparency creatives to CSV."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from adrenalin.common.geo import region_to_geo_id
from adrenalin.common.http import Blocked
from adrenalin.common.ocr import OcrUnavailable
from adrenalin.common.ocr.codex import DEFAULT_VISION_MODEL
from adrenalin.common.output import EntityCollision
from adrenalin.common.pipeline import print_to_stderr
from adrenalin.common.tool import Tool
from adrenalin.tools.ads_transparency import api

HELP = "Export one Google Ads Transparency advertiser's creatives and ad copy to CSV"


def _iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from None


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("advertiser_id", help="advertiser ID, e.g. AR04752318142645010433")
    p.add_argument("--region", default="anywhere", help="ISO alpha-2 country code (IN, US) or 'anywhere' (default)")
    p.add_argument("--start", type=_iso_date, help="first day of the shown period, YYYY-MM-DD (inclusive)")
    p.add_argument("--end", type=_iso_date, help="last day of the shown period, YYYY-MM-DD (inclusive)")
    p.add_argument("--days", type=int, help="shown in the last N days (ending today); excludes --start/--end")
    p.add_argument("--variations", action="store_true", help="one row per variation (one extra request per creative)")
    p.add_argument("--vision", action="store_true", help="send every image preview to Codex, not only OCR failures")
    p.add_argument("--vision-model", default=DEFAULT_VISION_MODEL,
                   help=f"Codex model for vision (default {DEFAULT_VISION_MODEL}, or $ADRENALIN_VISION_MODEL)")
    p.add_argument("--vision-limit", type=int, default=200, help="max Codex calls per run (default 200)")
    p.add_argument("--all", action="store_true", dest="read_all",
                   help="read every Creative's copy, even when the Inventory is large enough to sample")
    p.add_argument("--sampling", choices=("stratified", "novelty"), default="stratified",
                   help="representative stratified sample (default), or separate novelty discovery")
    p.add_argument("--coverage", type=float, default=0.95,
                   help="novelty mode only: legacy repetition-score stopping threshold, not population Coverage")
    p.add_argument("--max-ads", type=int, metavar="N",
                   help="representative sample size (default 2000), minimum 3 per group may increase it; novelty mode: read cap")
    p.add_argument("--no-countries", action="store_false", dest="countries",
                   help="skip Shown countries (one extra request per Creative in the Copy sample)")
    p.add_argument("--solve-captcha", action="store_true", help="show Chrome only when you need to solve a Google CAPTCHA")
    p.add_argument("--wait-on-block", action="store_true", help="wait out Google blocks instead of exiting")
    p.add_argument("--stage", choices=("list", "fetch", "extract", "export"), help="run one stage only")
    p.add_argument("--out", type=Path, default=Path("out"), help="output root (default out/)")
    p.add_argument("--proxy", action="append", default=[], dest="proxies", metavar="URL",
                   help="proxy URL; repeat to rotate per request")
    p.add_argument("-p", "--progress", action="store_true", help="show progress bars with rates and ETA")
    p.set_defaults(_parser=p)


def _hhmm(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%H:%M}"


def run(args: argparse.Namespace) -> int:
    parser: argparse.ArgumentParser = args._parser
    start, end = args.start, args.end
    if args.days is not None:
        if start or end:
            parser.error("--days cannot be combined with --start or --end")
        if args.days < 1:
            parser.error("--days must be at least 1")
        end = date.today()
        start = end - timedelta(days=args.days - 1)
    if start and end and start > end:
        parser.error(f"--start {start} is after --end {end}")
    try:
        region_to_geo_id(args.region)
    except ValueError as e:
        parser.error(str(e))
    if args.vision_limit < 0:
        parser.error("--vision-limit must be 0 or more")
    if not 0 < args.coverage < 1:
        parser.error("--coverage must be between 0 and 1 (exclusive)")
    if args.max_ads is not None and args.max_ads < 1:
        parser.error("--max-ads must be at least 1")

    options = api.ScrapeOptions(
        advertiser_id=args.advertiser_id, region=args.region, start=start, end=end,
        variations=args.variations, vision=args.vision, vision_model=args.vision_model,
        vision_limit=args.vision_limit, wait_on_block=args.wait_on_block, stage=args.stage,
        solve_captcha=args.solve_captcha,
        out=args.out, proxies=tuple(args.proxies), progress=args.progress,
        sampling_mode=args.sampling, read_all=args.read_all, coverage=args.coverage, max_ads=args.max_ads,
        countries=args.countries,
    )
    def log(message: str) -> None:
        if not api.is_outcome_line(message):  # printed below, once, with what to do next
            print_to_stderr(message)

    try:
        res = api.scrape(options, log=log)
    except Blocked as e:
        print(f"blocked until {_hhmm(e.until)}; rerun the same command to continue", file=sys.stderr)
        return 2
    except (ValueError, EntityCollision, OcrUnavailable, OSError, requests.RequestException) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"{res.csv_path} ({res.rows} rows)")
    code = 0
    if res.failed or res.errors:
        counts = ", ".join(f"{stage}: {n}" for stage, n in res.failed.items())
        if res.errors:
            counts = ", ".join(filter(None, [counts, f"errors: {len(res.errors)}"]))
        print(f"failed items ({counts}); rerun to retry", file=sys.stderr)
        code = 1
    if res.blocked_until is not None:
        print(f"{api.blocked_message(res.blocked_until)}; rerun the same command to continue", file=sys.stderr)
        code = 2
    if res.countries_pending:
        print(api.countries_message(res.countries_pending), file=sys.stderr)
        code = code or 3
    if not res.listing_complete:
        print(api.incomplete_message(res), file=sys.stderr)
        code = code or 3
    return code


TOOL = Tool("ads-transparency", HELP, add_arguments, run)
