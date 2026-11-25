import copy
import csv
import hashlib

from adrenalin.common.pipeline import WorkQueue
from adrenalin.tools.ads_transparency.api import is_outcome_line
from adrenalin.tools.ads_transparency.countries import COUNTRIES, COUNTRY_FIELDS, country_targets
from adrenalin.tools.ads_transparency.sample_queue import SampleTally

from .test_api import DETAIL, FakeGoogle, read_rows, response, run_dir, run_scrape, summary_of


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


def test_novelty_country_weights_match_copy_weight_total(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="novelty", max_ads=5, countries=True)
    copy_total = sum(float(r["sample_weight"]) for r in read_rows(res) if r["variation"] == "1" and r["sample_weight"])
    per_creative = {r["creative_id"]: r["sample_weight"] for r in country_csv(res)}
    assert len(per_creative) == 5
    assert abs(sum(float(w) for w in per_creative.values() if w) - copy_total) < 1e-6


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
