import hashlib

from adrenalin.common.pipeline import WorkQueue
from adrenalin.tools.ads_transparency.countries import COUNTRIES, country_targets
from adrenalin.tools.ads_transparency.sample_queue import SampleTally

from .test_api import FakeGoogle, run_dir, run_scrape, summary_of


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
