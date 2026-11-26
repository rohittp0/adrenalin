import json
from pathlib import Path

import pytest

from adrenalin.cli import discover_tools, main
from adrenalin.tools.meta_ad_library import api
from adrenalin.tools.meta_ad_library.api import MetaResult

from meta_fakes import FakeMeta, PAGE_ID, load, page


def result(**kw):
    base = dict(csv_path=Path("out/x/ads.csv"), rows=3, blocked_until=None, failed={}, errors=[],
                listing_complete=True, listed=3, summary_path=Path("out/x/summary.json"))
    return MetaResult(**{**base, **kw})


def capture(monkeypatch, res=None):
    seen = []
    monkeypatch.setattr(api, "scrape", lambda options, **kw: seen.append(options) or (res or result()))
    return seen


def test_tool_discovered():
    assert "meta-ad-library" in discover_tools()


def test_cli_passes_options(monkeypatch, capsys):
    seen = capture(monkeypatch)
    url = "https://www.facebook.com/ads/library/?view_all_page_id=120740541030"
    assert main(["meta-ad-library", url, "--region", "IN", "--days", "7", "--status", "active", "--variations",
                 "--eu-reach", "--ocr", "--stage", "fetch", "--out", "/tmp/o", "-p"]) == 0
    o = seen[0]
    assert (o.page_id, o.region, o.status, o.stage) == (PAGE_ID, "IN", "active", "fetch")
    assert o.variations and o.eu_reach and o.ocr and o.progress and o.out == Path("/tmp/o")
    assert o.start is not None and o.end is not None
    assert "out/x/ads.csv (3 rows)" in capsys.readouterr().out


def test_cli_rejects_profile_url(monkeypatch):
    capture(monkeypatch)
    with pytest.raises(SystemExit) as e:
        main(["meta-ad-library", "https://www.facebook.com/makemytrip"])
    assert e.value.code == 2


def test_cli_exit_codes(monkeypatch, capsys, tmp_path, make_session):
    capture(monkeypatch, result(blocked_until=1_900_000_000.0))
    assert main(["meta-ad-library", PAGE_ID]) == 2
    assert "blocked until" in capsys.readouterr().err
    capture(monkeypatch, result(listing_complete=False, listed=1234))
    assert main(["meta-ad-library", PAGE_ID]) == 3
    assert "INCOMPLETE: listed 1,234 Creatives; rerun the same command to continue" in capsys.readouterr().err
    capture(monkeypatch, result(failed={"fetch": 2}))
    assert main(["meta-ad-library", PAGE_ID]) == 1
    monkeypatch.undo()
    # a real run: the Page's first request is refused, then a changed query
    monkeypatch.setattr(api, "make_session", lambda *a, **k: make_session(FakeMeta({None: load("doc_not_found.json")})))
    assert main(["meta-ad-library", PAGE_ID, "--out", str(tmp_path / "o")]) == 1
    assert "SEARCH_DOC_ID" in capsys.readouterr().err
    monkeypatch.setattr(api, "make_session", lambda *a, **k: make_session(FakeMeta({}, blocked=True)))
    assert main(["meta-ad-library", PAGE_ID, "--out", str(tmp_path / "o")]) == 2


def test_doc_not_found_is_one_clear_message(monkeypatch, capsys, tmp_path, make_session):
    out = tmp_path / "o"
    (out / ".cache").mkdir(parents=True)
    (out / ".cache" / "meta_pages.json").write_text(json.dumps({PAGE_ID: "Make My Trip"}))  # the pipeline meets it
    monkeypatch.setattr(api, "make_session", lambda *a, **k: make_session(FakeMeta({None: load("doc_not_found.json")})))
    assert main(["meta-ad-library", PAGE_ID, "--out", str(out)]) == 1
    captured = capsys.readouterr()
    lines = [line for line in (captured.out + captured.err).splitlines() if line.strip()]
    assert lines == [f"error: {api.DOC_NOT_FOUND}"]
