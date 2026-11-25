import copy
import csv
import hashlib
from datetime import date

from adrenalin.common.pipeline import WorkQueue
from adrenalin.tools.ads_transparency.api import is_outcome_line
from adrenalin.tools.ads_transparency.countries import COUNTRIES, COUNTRY_FIELDS, country_targets
from adrenalin.tools.ads_transparency.sample_queue import SampleTally

from .test_api import DETAIL, Clock, FakeGoogle, make_session, read_rows, response, run_dir, run_scrape, summary_of


def countries_items(tmp_path):
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        return {i.key: i for i in q.items(COUNTRIES)}
    finally:
        q.close()


def test_countries_fetched_for_representative_sample(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    assert not res.failed and not res.errors
    selected = summary_of(res)["sampling"]["cohort"]["selected"]
    items = countries_items(tmp_path)
    assert set(items) == set(selected) and fake.calls["detail"] == len(selected)
    assert all(i.status == "done" and "shown_countries" in i.result for i in items.values())


def test_countries_reuse_detail_response_with_variations(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, variations=True, countries=True)
    assert not res.failed and not res.errors
    selected = summary_of(res)["sampling"]["cohort"]["selected"]
    q = WorkQueue(run_dir(tmp_path, "anywhere_all_all_variations") / "state.sqlite3")
    try:
        details = len(q.items("detail"))
        done = [i for i in q.items(COUNTRIES) if i.status == "done"]
    finally:
        q.close()
    assert fake.calls["detail"] == details and len(done) == len(selected)


def test_no_countries_makes_no_detail_requests(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=False)
    assert fake.calls["detail"] == 0 and not countries_items(tmp_path)


def test_novelty_countries_capped_at_max_ads(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="novelty", max_ads=5, countries=True)
    assert fake.calls["detail"] == 5 and len(countries_items(tmp_path)) == 5


def test_rerun_backfills_countries_without_rereading_copy(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=False)
    before = dict(fake.calls)
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    assert not res.failed and not res.errors
    selected = summary_of(res)["sampling"]["cohort"]["selected"]
    assert fake.calls["detail"] - before.get("detail", 0) == len(selected)
    assert fake.calls["content_js"] == before.get("content_js", 0)
    assert fake.calls["simgad"] == before.get("simgad", 0)


def test_country_targets_novelty_is_lowest_hash_capped():
    admitted = {f"CR{i}" for i in range(1, 10)}
    expected = sorted(admitted, key=lambda c: hashlib.sha1(c.encode()).hexdigest())[:3]
    assert country_targets(SampleTally(), admitted, 3) == expected


def test_country_targets_stratified_is_the_representative_sample():
    tally = SampleTally(representative={"CR2", "CR1"})
    assert country_targets(tally, {"CR1", "CR2", "CR3"}, 1) == ["CR1", "CR2"]


def country_csv(res):
    return list(csv.DictReader((res.csv_path.parent / "countries.csv").open(encoding="utf-8")))


def test_countries_csv_rows_and_weights(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="stratified", max_ads=10, countries=True)
    rows = country_csv(res)
    assert list(rows[0]) == list(COUNTRY_FIELDS)
    assert {r["status"] for r in rows} <= {"shown", "none_reported"} and res.countries_pending == 0
    assert any(r["status"] == "shown" and r["country"] == "IN" and r["last_shown"] == "2026-11-19" for r in rows)
    per_creative = {r["creative_id"]: float(r["sample_weight"]) for r in rows}
    assert set(per_creative) == set(summary_of(res)["sampling"]["cohort"]["selected"])
    assert abs(sum(per_creative.values()) - 40) < 1e-6
    assert rows == sorted(rows, key=lambda r: (r["creative_id"], r["country"]))


def test_novelty_country_weights_cover_at_most_the_inventory(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="novelty", max_ads=5, countries=True)
    per_creative = {r["creative_id"]: r["sample_weight"] for r in country_csv(res)}
    assert len(per_creative) == 5 and all(per_creative.values())
    # post-stratified: each fetched Creative stands for its group's Inventory / fetched; groups without one drop out
    assert 5 <= sum(float(w) for w in per_creative.values()) <= 40 + 1e-6


def test_none_reported_when_detail_has_no_field_17(tmp_path):
    fake = FakeGoogle()
    body = copy.deepcopy(DETAIL)
    del body["1"]["17"]
    fake.detail_reply = response(body=body)
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    rows = country_csv(res)
    assert {r["status"] for r in rows} == {"none_reported"} and all(r["country"] == "" for r in rows)
    assert len(rows) == len({r["creative_id"] for r in rows})


def test_countries_block_leaves_pending_rows(tmp_path):
    fake = FakeGoogle()
    fake.detail_reply = response(302, b"", headers={"Location": "https://www.google.com/sorry/index"})
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    selected = summary_of(res)["sampling"]["cohort"]["selected"]
    assert res.blocked_until is not None and res.countries_pending == len(selected)
    rows = country_csv(res)
    assert {r["status"] for r in rows} == {"pending"} and all(r["country"] == "" for r in rows)
    assert any(r["headline"] for r in read_rows(res))  # copy was still read and exported


def test_stage_export_writes_countries_without_requests(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    before = fake.calls["detail"]
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True, stage="export")
    assert fake.calls["detail"] == before and {r["status"] for r in country_csv(res)} <= {"shown", "none_reported"}


def test_no_countries_writes_no_countries_csv(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="stratified", max_ads=10, countries=False)
    assert not (res.csv_path.parent / "countries.csv").exists()


def test_pending_countries_line_is_an_outcome_line():
    assert is_outcome_line("1,234 Creatives still need Shown countries; rerun the same command to continue")
    assert not is_outcome_line("1,234 Creatives still need Shown countries")


# -- final review fixes --------------------------------------------------------

from adrenalin.tools.ads_transparency.countries import (  # noqa: E402
    countries_pending, country_rows, country_weights, queue_countries)
from adrenalin.tools.ads_transparency.rpc import Query  # noqa: E402
from adrenalin.tools.ads_transparency.sample_queue import SampleGroup  # noqa: E402


def _novelty_tally():
    """Two groups of 1,000: A had 500 copy reads (novelty rounds), B only 100."""
    tally = SampleTally()
    tally.groups = {"A": SampleGroup(held=["h"] * 500, admitted=500, processed=500),
                    "B": SampleGroup(held=["h"] * 900, admitted=100, processed=100)}
    for g, n in (("A", 500), ("B", 100)):
        for i in range(n):
            tally.group_of[f"{g}{i}"] = g
            tally.processed.add(f"{g}{i}")
    return tally


def _item(key, status):
    from adrenalin.common.pipeline import Item
    return Item(COUNTRIES, key, {"creative_id": key}, {"shown_countries": []} if status == "done" else None, status,
                None, 1)


def test_novelty_country_weights_are_post_stratified():
    tally = _novelty_tally()
    items = [_item(f"A{i}", "done") for i in range(100)] + [_item(f"B{i}", "done") for i in range(100)]
    items.append(_item("A100", "pending"))
    w = country_weights(items, tally, sampled=True)
    assert w["A0"] == 10 and w["B0"] == 10  # 1,000 / 100 fetched in each group
    assert w["A100"] == ""  # pending: no weight in novelty mode
    assert abs(sum(v for v in w.values() if v != "") - 2000) < 1e-6


def test_exhausted_countries_items_are_not_pending(tmp_path):
    path = tmp_path / "state.sqlite3"
    for _ in range(3):
        q = WorkQueue(path)
        if q.item(COUNTRIES, "CR1") is None:
            q.put(COUNTRIES, "CR1", {"creative_id": "CR1"})
        q.claim(COUNTRIES)
        q.fail(COUNTRIES, "CR1", "ValueError: gone")
        q.close()
    q = WorkQueue(path)
    try:
        q.put(COUNTRIES, "CR2", {"creative_id": "CR2"})
        assert countries_pending(q, ["CR1", "CR2"]) == 1  # CR1 used up its retries; a rerun can't fetch it
    finally:
        q.close()


def _queue_with_fetch_items(tmp_path, n):
    q = WorkQueue(tmp_path / "state.sqlite3")
    for i in range(n):
        q.put("fetch", f"CR{i}#1", {"creative_id": f"CR{i}", "first_shown": "2026-01-01", "last_shown": "2026-01-02"})
    return q


def test_novelty_countries_wait_for_the_full_listing(tmp_path):
    q = _queue_with_fetch_items(tmp_path, 9)
    try:
        query = Query("AR1", None, None, None)
        assert queue_countries(q, False, query, date(2026, 1, 3), 3) == 0  # listing not done: targets would drift
        q.set_meta("list_done", "2026-01-03")
        assert queue_countries(q, False, query, date(2026, 1, 3), 3) == 3
    finally:
        q.close()


def test_country_rows_cover_current_targets_only(tmp_path):
    q = _queue_with_fetch_items(tmp_path, 2)
    try:
        q.put(COUNTRIES, "OLD", {"creative_id": "OLD"})  # queued by an earlier run with other options
        q.put(COUNTRIES, "CR0", {"creative_id": "CR0"})
        q.complete(COUNTRIES, "CR0", {"shown_countries": [{"1": 2840, "5": 20261122}]})
        tally = SampleTally(group_of={"CR0": "g", "CR1": "g"}, representative={"CR0", "CR1"}, design_weights={"g": 2.0})
        rows = country_rows(q, tally, sampled=True, targets=["CR0", "CR1"])
        assert [(r["creative_id"], r["status"], r["sample_weight"]) for r in rows] == [
            ("CR0", "shown", 2.0), ("CR1", "pending", 2.0)]
        assert countries_pending(q, ["CR0", "CR1"]) == 1
    finally:
        q.close()


def test_rerun_after_countries_block_finishes_them(tmp_path):
    fake = FakeGoogle()
    fake.detail_reply = response(302, b"", headers={"Location": "https://www.google.com/sorry/index"})
    clock = Clock()
    run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock), sampling_mode="stratified", max_ads=10,
               countries=True)
    fake.detail_reply = None
    clock.now += 7200  # past the cooldown
    res = run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock), sampling_mode="stratified",
                     max_ads=10, countries=True)
    assert res.countries_pending == 0 and {r["status"] for r in country_csv(res)} <= {"shown", "none_reported"}
