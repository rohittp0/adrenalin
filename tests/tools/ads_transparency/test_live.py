import csv
import time
from datetime import date, timedelta

import pytest

from adrenalin.tools.ads_transparency.api import ScrapeOptions, scrape

ADVERTISER_ID = "AR04752318142645010433"  # Lascade LLC
METHODS = {"content_js", "apple_vision", "tesseract", "codex"}


@pytest.mark.live
def test_live_lascade_last_7_days(tmp_path):
    today = date.today()
    t0 = time.monotonic()
    res = scrape(ScrapeOptions(ADVERTISER_ID, region="IN", start=today - timedelta(days=6), end=today, out=tmp_path))
    print(f"\nlive run: {res.rows} rows in {time.monotonic() - t0:.0f}s, blocked_until={res.blocked_until}, "
          f"failed={res.failed}, errors={res.errors}")
    assert res.blocked_until is None, "Google blocked the run; rerun later"
    assert res.rows > 0
    with res.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert any(r["headline"] for r in rows)
    for r in rows:
        # empty only for content.js parse misses
        assert r["extraction_method"] in METHODS | {""}
