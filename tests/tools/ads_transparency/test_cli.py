from datetime import date, timedelta
from pathlib import Path

import pytest

from adrenalin.cli import main
from adrenalin.tools.ads_transparency import api
from adrenalin.tools.ads_transparency.api import ScrapeResult


def capture(monkeypatch, result=None):
    seen = []

    def fake_scrape(options, **kw):
        seen.append(options)
        return result or ScrapeResult(Path("out/x/ads.csv"), 3, None, {}, 0)

    monkeypatch.setattr(api, "scrape", fake_scrape)
    return seen


def test_cli_parses_days_and_region(monkeypatch, capsys):
    seen = capture(monkeypatch)
    assert main(["ads-transparency", "AR1", "--region", "IN", "--days", "7"]) == 0
    opts = seen[0]
    today = date.today()
    assert (opts.advertiser_id, opts.region) == ("AR1", "IN")
    assert opts.start == today - timedelta(days=6) and opts.end == today
    out = capsys.readouterr().out
    assert "out/x/ads.csv" in out and "3" in out


def test_cli_passes_every_option(monkeypatch):
    seen = capture(monkeypatch)
    assert main(["ads-transparency", "AR1", "--start", "2026-11-01", "--end", "2026-11-18", "--variations",
                 "--vision", "--vision-model", "m1", "--vision-limit", "5", "--wait-on-block",
                 "--stage", "fetch", "--out", "/tmp/o", "--proxy", "http://p1", "--proxy", "http://p2"]) == 0
    o = seen[0]
    assert (o.start, o.end) == (date(2026, 11, 1), date(2026, 11, 18))
    assert o.variations and o.vision and o.wait_on_block
    assert (o.vision_model, o.vision_limit, o.stage) == ("m1", 5, "fetch")
    assert o.out == Path("/tmp/o") and o.proxies == ("http://p1", "http://p2")
    assert o.region == "anywhere"
    assert not o.progress


def test_cli_progress_flag(monkeypatch):
    seen = capture(monkeypatch)
    assert main(["ads-transparency", "AR1", "-p"]) == 0
    assert seen[0].progress


def test_cli_rejects_days_with_start():
    with pytest.raises(SystemExit) as e:
        main(["ads-transparency", "AR1", "--days", "7", "--start", "2026-11-01"])
    assert e.value.code == 2


def test_cli_rejects_bad_region_and_reversed_dates(monkeypatch):
    capture(monkeypatch)
    for argv in (["--region", "XX"], ["--start", "2026-11-02", "--end", "2026-11-01"], ["--days", "0"]):
        with pytest.raises(SystemExit) as e:
            main(["ads-transparency", "AR1", *argv])
        assert e.value.code == 2


def test_cli_blocked_exits_2(monkeypatch, capsys):
    capture(monkeypatch, ScrapeResult(Path("out/x/ads.csv"), 5, 1_900_000_000.0, {}, 0))
    assert main(["ads-transparency", "AR1"]) == 2
    err = capsys.readouterr().err
    assert "blocked until" in err and "rerun" in err


def test_cli_failed_items_exit_1(monkeypatch, capsys):
    capture(monkeypatch, ScrapeResult(Path("out/x/ads.csv"), 5, None, {"fetch": 2, "extract": 1}, 0))
    assert main(["ads-transparency", "AR1"]) == 1
    err = capsys.readouterr().err
    assert "fetch: 2" in err and "extract: 1" in err and "rerun to retry" in err


def test_cli_scrape_error_exits_1(monkeypatch, capsys):
    def boom(options, **kw):
        raise ValueError("No creatives found for AR1")

    monkeypatch.setattr(api, "scrape", boom)
    assert main(["ads-transparency", "AR1"]) == 1
    assert "No creatives found for AR1" in capsys.readouterr().err
