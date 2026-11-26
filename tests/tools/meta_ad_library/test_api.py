import csv
import json
import logging
from datetime import date

import pytest

from adrenalin.tools.meta_ad_library.api import (
    CSV_FIELDS,
    EU_COUNTRIES,
    MetaOptions,
    parse_page_arg,
    scrape,
)
from adrenalin.tools.meta_ad_library.parse import parse_detail, parse_search_page

from meta_fakes import FakeMeta, response, PAGE_ID, load, page

DCO_ID = "1535930091652737"
TWO_PAGES = {None: page("first_page.json", "C2"), "C2": page("search_page.json")}


def run(tmp_path, make_session, pages=None, fake=None, **kw):
    fake = fake or FakeMeta(pages or TWO_PAGES)
    res = scrape(MetaOptions(PAGE_ID, out=tmp_path / "out", **kw), session=make_session(fake), poll=0.005)
    return res, fake


def read(res):
    with res.csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fixture_creatives():
    return {c.library_id: c for n in ("first_page.json", "search_page.json")
            for c in parse_search_page(load(n)).creatives}


def test_export_rows(tmp_path, make_session):
    res, _ = run(tmp_path, make_session)
    rows = read(res)
    assert res.rows == len(rows) == 40 and res.listing_complete and res.listed == 40
    assert res.failed == {} and res.errors == [] and res.blocked_until is None
    with res.csv_path.open(encoding="utf-8") as f:
        assert next(csv.reader(f)) == CSV_FIELDS
    known = fixture_creatives()
    assert {r["library_id"] for r in rows} == set(known)
    for r in rows:
        c = known[r["library_id"]]
        assert r["is_active"] == str(c.is_active).lower()
        assert r["ad_library_url"] == f"https://www.facebook.com/ads/library/?id={r['library_id']}"
        assert r["page_id"] == PAGE_ID and r["variation"] == "1"
    dco = next(r for r in rows if r["library_id"] == DCO_ID)
    assert dco["title"] == known[DCO_ID].variations[0].title
    assert dco["image_files"] and all((res.csv_path.parents[2] / f).is_file() for f in dco["image_files"].split(" | "))
    assert res.csv_path.parent.name == "anywhere_all_all_all"
    summary = json.loads(res.summary_path.read_text())
    assert summary["listed"] == 40 and summary["rows"] == 40 and summary["listing_complete"] is True
    assert summary["inactive_listed"] == sum(not c.is_active for c in known.values())
    assert "list" in summary["stage_seconds"]


def test_variations_rows(tmp_path, make_session):
    res, _ = run(tmp_path, make_session, variations=True)
    rows = read(res)
    dco = [r for r in rows if r["library_id"] == DCO_ID]
    assert [r["variation"] for r in dco] == ["1", "2"]
    assert len(rows) == sum(len(c.variations) for c in fixture_creatives().values())
    assert res.listed == 40  # Creatives, not rows
    assert res.csv_path.parent.name.endswith("_variations")


def test_eu_reach_columns(tmp_path, make_session):
    res, _ = run(tmp_path, make_session, {None: page("search_page.json")}, eu_reach=True)
    row = read(res)[0]
    assert row["eu_total_reach"] == "22"
    by_country = json.loads(row["eu_reach_by_country"])
    assert isinstance(by_country, dict) and by_country
    detail = parse_detail(load("detail_eu.json"))
    assert (row["uk_total_reach"], row["payer"], row["beneficiary"]) == (
        "" if detail.uk_total is None else str(detail.uk_total), detail.payer, detail.beneficiary)


def test_ocr_text_survives_a_rerun_without_ocr(tmp_path, make_session):
    from adrenalin.common.ocr import OcrLine, OcrResult

    reader = lambda path: OcrResult([OcrLine("Book now", 0.9, (0, 0, 1, 0.1))], "fake")  # noqa: E731
    fake = FakeMeta({None: page("search_page.json")})
    res = scrape(MetaOptions(PAGE_ID, out=tmp_path / "out", ocr=True), session=make_session(fake), ocr=reader,
                 poll=0.005)
    assert any(r["image_text"] == "Book now" for r in read(res))
    res = scrape(MetaOptions(PAGE_ID, out=tmp_path / "out", stage="export"), session=make_session(fake))
    assert any(r["image_text"] == "Book now" for r in read(res))
    res = scrape(MetaOptions(PAGE_ID, out=tmp_path / "out"), session=make_session(fake), poll=0.005)
    assert any(r["image_text"] == "Book now" for r in read(res))  # a rerun without --ocr keeps the text


def test_incomplete_listing(tmp_path, make_session):
    pages = {None: page("first_page.json", "C2"), "C2": json.dumps({"errors": [{"message": "bad cursor"}]})}
    res, _ = run(tmp_path, make_session, pages)
    assert not res.listing_complete and res.rows == res.listed == 30 and res.failed == {"list": 1}


def test_stage_export_runs_nothing(tmp_path, make_session):
    run(tmp_path, make_session)
    fake = FakeMeta({})
    res = scrape(MetaOptions(PAGE_ID, out=tmp_path / "out", stage="export"), session=make_session(fake))
    assert res.rows == 40 and res.listing_complete and fake.requests == []
    assert "list" in json.loads(res.summary_path.read_text())["stage_seconds"]


def test_page_name_cached_and_used_for_entity(tmp_path, make_session):
    res, fake = run(tmp_path, make_session)
    name = next(iter(fixture_creatives().values())).page_name
    assert res.csv_path.parents[2].name != PAGE_ID
    cache = json.loads((tmp_path / "out" / ".cache" / "meta_pages.json").read_text())
    assert cache == {PAGE_ID: name}
    lookups = fake.requests.count("https://www.facebook.com/api/graphql/")
    res2, fake2 = run(tmp_path, make_session, status="active")
    assert fake2.requests.count("https://www.facebook.com/api/graphql/") == 2  # no lookup this time, pages 1 and 2
    assert lookups == 3


def test_doc_not_found_logs_once(tmp_path, make_session, caplog):
    caplog.set_level(logging.INFO, logger="adrenalin")
    pages = {None: load("doc_not_found.json")}
    # the Page name is known: the lookup is skipped and the pipeline meets the changed query
    cache = tmp_path / "out" / ".cache"
    cache.mkdir(parents=True)
    (cache / "meta_pages.json").write_text(json.dumps({PAGE_ID: "Make My Trip"}))
    res, _ = run(tmp_path, make_session, pages)
    assert res.errors and "DocNotFound" in res.errors[0]
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert sum("SEARCH_DOC_ID" in m for m in msgs) == 1


def test_doc_not_found_in_page_lookup(tmp_path, make_session):
    with pytest.raises(ValueError, match="SEARCH_DOC_ID"):
        run(tmp_path, make_session, {None: load("doc_not_found.json")})


@pytest.mark.parametrize("region, noted", [("anywhere", False), ("IN", True), ("DE", False), ("de", False)])
def test_inactive_note_outside_the_eu(tmp_path, make_session, caplog, region, noted):
    caplog.set_level(logging.INFO, logger="adrenalin")
    run(tmp_path, make_session, {None: page("search_page.json")}, region=region)
    assert any("only partly listed" in r.getMessage() for r in caplog.records) is noted


def test_eu_countries():
    assert len(EU_COUNTRIES) == 27 and {"DE", "FR", "IE"} <= EU_COUNTRIES and "GB" not in EU_COUNTRIES


def test_page_arg_accepts_id_and_ad_library_url():
    assert parse_page_arg("120740541030") == "120740541030"
    assert parse_page_arg("https://www.facebook.com/ads/library/?view_all_page_id=120740541030&country=IN") \
        == "120740541030"


@pytest.mark.parametrize("text", ["https://www.facebook.com/makemytrip", "makemytrip", "", "12ab"])
def test_page_arg_rejects_profile_url(text):
    with pytest.raises(ValueError, match="expected a numeric Page ID or an Ad Library URL containing view_all_page_id="):
        parse_page_arg(text)


def test_bad_period_is_rejected(tmp_path, make_session):
    with pytest.raises(ValueError, match="after"):
        run(tmp_path, make_session, start=date(2026, 11, 2), end=date(2026, 11, 1))


def test_narrow_run_still_names_the_page_from_a_broad_lookup(tmp_path, make_session):
    empty = json.loads(page("search_page.json"))
    empty["data"]["ad_library_main"]["search_results_connection"]["edges"] = []
    seen = []

    class Narrow(FakeMeta):
        def __call__(self, method, url, data=None, **kw):
            v = json.loads(data["variables"]) if data else {}
            if "countries" in v:
                seen.append((v["countries"], v["activeStatus"]))
                if v["activeStatus"] != "all":
                    return response(json.dumps(empty))
            return super().__call__(method, url, data=data, **kw)

    fake = Narrow({None: page("search_page.json")})
    res = scrape(MetaOptions(PAGE_ID, out=tmp_path / "out", region="IN", status="inactive"),
                 session=make_session(fake), poll=0.005)
    assert res.rows == 0 and res.csv_path.parents[2].name == "makemytrip"
    assert seen[0] == (["ALL"], "all")


@pytest.mark.parametrize("kw", [{"region": "XX"}, {"stage": "bogus"}])
def test_unknown_region_and_stage_are_rejected(tmp_path, make_session, kw):
    with pytest.raises(ValueError):
        run(tmp_path, make_session, **kw)
