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


@pytest.mark.parametrize("phase", ["0.2", "1.5", "8"])
def test_cli_phase_reaches_session_factory(monkeypatch, tmp_path, phase):
    seen = []
    def factory(*args, **kwargs):
        seen.append(kwargs["phase"])
        raise ValueError("stop before network")
    monkeypatch.setattr(api, "make_session", factory)
    assert main(["ads-transparency", "AR1", "--phase", phase, "--out", str(tmp_path)]) == 1
    assert seen == [float(phase)]


@pytest.mark.parametrize("phase", ["0", "-1", "nan", "inf", "-inf"])
def test_phase_rejects_invalid_values(monkeypatch, tmp_path, phase):
    seen = capture(monkeypatch)
    with pytest.raises(SystemExit) as error:
        main(["ads-transparency", "AR1", f"--phase={phase}"])
    assert error.value.code == 2
    assert not seen
    with pytest.raises(ValueError, match="positive finite"):
        api.ScrapeOptions("AR1", phase=float(phase))
    with pytest.raises(ValueError, match="positive finite"):
        api.make_session(tmp_path, phase=float(phase))


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


def test_cli_stage_export_of_incomplete_run_exits_3(monkeypatch, capsys):
    capture(monkeypatch, ScrapeResult(Path("out/x/ads.csv"), 3, None, {}, 0, listing_complete=False,
                                      listed=3, estimate=10))
    assert main(["ads-transparency", "AR1", "--stage", "export"]) == 3
    assert "INCOMPLETE" in capsys.readouterr().err


def test_cli_sampling_options(monkeypatch):
    seen = capture(monkeypatch)
    assert main(["ads-transparency", "AR1"]) == 0
    assert (seen[0].read_all, seen[0].coverage, seen[0].max_ads) == (False, 0.95, None)
    assert main(["ads-transparency", "AR1", "--all", "--coverage", "0.9", "--max-ads", "100"]) == 0
    assert (seen[1].read_all, seen[1].coverage, seen[1].max_ads) == (True, 0.9, 100)


@pytest.mark.parametrize("args", [["--coverage", "1.5"], ["--coverage", "0"], ["--coverage", "1"],
                                  ["--max-ads", "0"]])
def test_cli_rejects_bad_coverage_and_max_ads(monkeypatch, args):
    capture(monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["ads-transparency", "AR1", *args])
    assert e.value.code == 2


def test_cli_countries_on_by_default_and_no_countries_flag(monkeypatch):
    seen = capture(monkeypatch)
    assert main(["ads-transparency", "AR1"]) == 0
    assert main(["ads-transparency", "AR1", "--no-countries"]) == 0
    assert seen[0].countries is True and seen[1].countries is False


def test_cli_pending_countries_exit_3(monkeypatch, capsys):
    capture(monkeypatch, ScrapeResult(Path("out/x/ads.csv"), 3, None, {}, 0, countries_pending=3))
    assert main(["ads-transparency", "AR1"]) == 3
    assert "3 Creatives still need Shown countries; rerun the same command to continue" in capsys.readouterr().err
