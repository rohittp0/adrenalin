"""`adrenalin meta-ad-library`: export one Facebook Page's Meta Ad Library Creatives to CSV."""
from __future__ import annotations

import argparse

from adrenalin.common.cli import add_period_arguments, add_run_arguments, finish, resolve_period, run_cli
from adrenalin.common.tool import Tool
from adrenalin.tools.meta_ad_library import api

HELP = "Export one Facebook Page's Meta Ad Library Creatives to CSV"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("page", help="Facebook Page ID, or an Ad Library URL containing view_all_page_id=<id>")
    p.add_argument("--status", choices=("all", "active", "inactive"), default="all",
                   help="which Creatives to list by running status (default all)")
    p.add_argument("--variations", action="store_true", help="one row per Variation of a DCO Creative")
    p.add_argument("--eu-reach", action="store_true", help="EU reach, payer and beneficiary (one request per Creative)")
    p.add_argument("--ocr", action="store_true", help="read the text in each Creative's images")
    p.add_argument("--stage", choices=("list", "fetch", "export"), help="run one stage only")
    add_period_arguments(p)
    add_run_arguments(p)
    p.set_defaults(_parser=p)


def run(args: argparse.Namespace) -> int:
    parser: argparse.ArgumentParser = args._parser
    start, end = resolve_period(args, parser)
    try:
        page_id = api.parse_page_arg(args.page)
    except ValueError as e:
        parser.error(str(e))
    options = api.MetaOptions(
        page_id=page_id, region=args.region, status=args.status, start=start, end=end,
        variations=args.variations, eu_reach=args.eu_reach, ocr=args.ocr, wait_on_block=args.wait_on_block,
        solve_captcha=args.solve_captcha, stage=args.stage, out=args.out, proxies=tuple(args.proxies),
        progress=args.progress)

    def body() -> int:
        res = api.scrape(options)
        if res.doc_not_found:
            return 1  # the scrape logged the one line that matters: update the doc_ids
        print(f"{res.csv_path} ({res.rows} rows)")
        return finish(failed=res.failed, errors=res.errors, blocked_until=res.blocked_until,
                      incomplete_message=None if res.listing_complete else api.incomplete_message(res))

    return run_cli(args, body)


TOOL = Tool("meta-ad-library", HELP, add_arguments, run)
