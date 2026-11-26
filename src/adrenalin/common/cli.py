"""Shared CLI pieces for tools: run flags, the Shown period, outcome lines and exit codes.

Exit codes for every tool: 0 ok, 1 failed items or errors, 2 blocked, 3 incomplete or pending."""
from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from adrenalin.common.geo import region_to_geo_id
from adrenalin.common.http import Blocked
from adrenalin.common.output import EntityCollision
from adrenalin.common.runlog import console_logging

log = logging.getLogger(__name__)


def iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from None


def add_run_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--solve-captcha", action="store_true",
                   help="show Chrome only when you need to solve a CAPTCHA or other challenge in a visible Chrome window")
    p.add_argument("--wait-on-block", action="store_true", help="wait out blocks instead of exiting")
    p.add_argument("--out", type=Path, default=Path("out"), help="output root (default out/)")
    p.add_argument("--proxy", action="append", default=[], dest="proxies", metavar="URL",
                   help="proxy URL; repeat to rotate per request")
    p.add_argument("-p", "--progress", action="store_true", help="show progress bars with rates and ETA")
    p.add_argument("-v", "--verbose", action="store_true", help="also print debug messages")


def add_period_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--region", default="anywhere", help="ISO alpha-2 country code (IN, US) or 'anywhere' (default)")
    p.add_argument("--start", type=iso_date, help="first day of the shown period, YYYY-MM-DD (inclusive)")
    p.add_argument("--end", type=iso_date, help="last day of the shown period, YYYY-MM-DD (inclusive)")
    p.add_argument("--days", type=int, help="shown in the last N days (ending today); excludes --start/--end")


def resolve_period(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[date | None, date | None]:
    """Validate region and Shown period flags (exits with a usage error) and return (start, end)."""
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
    return start, end


def exit_code(*, failed: Mapping[str, int], errors: Sequence[str], blocked_until: float | None,
              incomplete: bool) -> int:
    if blocked_until is not None:
        return 2
    if failed or errors:
        return 1
    return 3 if incomplete else 0


def _hhmm(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%H:%M}"


def finish(*, failed: Mapping[str, int], errors: Sequence[str], blocked_until: float | None,
           incomplete_message: str | None = None, pending_messages: Sequence[str] = ()) -> int:
    """Log the run's outcome lines (plain INFO, so stderr text is verbatim) and return the exit code."""
    if failed or errors:
        counts = ", ".join(f"{stage}: {n}" for stage, n in failed.items())
        if errors:
            counts = ", ".join(filter(None, [counts, f"errors: {len(errors)}"]))
        log.info("failed items (%s); rerun to retry", counts)
    if blocked_until is not None:
        log.info("blocked until %s; rerun the same command to continue", _hhmm(blocked_until))
    for message in pending_messages:
        log.info("%s", message)
    if incomplete_message:
        log.info("%s", incomplete_message)
    return exit_code(failed=failed, errors=errors, blocked_until=blocked_until,
                     incomplete=bool(incomplete_message or pending_messages))


def run_cli(args: argparse.Namespace, body: Callable[[], int], *,
            user_errors: tuple[type[BaseException], ...] = ()) -> int:
    """Run `body` with console logging and map its exceptions to exit codes (Blocked 2, errors 1)."""
    with console_logging(args.verbose):
        try:
            return body()
        except Blocked as e:
            log.info("blocked until %s; rerun the same command to continue", _hhmm(e.until))
            return 2
        except (ValueError, EntityCollision, OSError, requests.RequestException, *user_errors) as e:
            log.error("%s", e)
            return 1
