import json
from adrenalin.tools.ads_transparency.api import ScrapeOptions
from adrenalin.tools.ads_transparency.sampling import stratified_allocation
from .test_api import FakeGoogle, run_scrape, read_rows, summary_of


def test_allocation_minimums_capacity_and_proportions():
    assert stratified_allocation({'a': 900, 'b': 100}, 100) == {'a': 88, 'b': 12}
    assert stratified_allocation({'a': 1, 'b': 4, 'c': 20}, 2) == {'a': 1, 'b': 3, 'c': 3}
    assert stratified_allocation({'a': 1, 'b': 4}, 100) == {'a': 1, 'b': 4}
    assert stratified_allocation({}, 20) == {}


def test_default_stratified_finishes_without_coverage_and_resumes(tmp_path):
    assert ScrapeOptions('AR1').sampling_mode == 'stratified'
    first = run_scrape(tmp_path, FakeGoogle(), sampling_mode='stratified', max_ads=10)
    assert not first.failed and not first.errors
    summary = summary_of(first)
    assert summary['coverage'] is None
    cohort = summary['sampling']['cohort']
    assert len(cohort['selected']) == sum(cohort['quotas'].values())
    rows = read_rows(first)
    selected = [r for r in rows if r['sample_role'] == 'representative']
    assert {r['creative_id'] for r in selected} == set(cohort['selected'])
    assert abs(sum(float(r['sample_weight']) for r in selected) - 40) < 1e-6
    assert all(not r['sample_weight'] for r in rows if r['sample_role'] != 'representative')
    second = run_scrape(tmp_path, FakeGoogle(), sampling_mode='stratified', max_ads=10)
    assert summary_of(second)['sampling']['cohort'] == cohort


def test_legacy_reads_are_retained_but_not_used_to_choose_sample(tmp_path):
    old = run_scrape(tmp_path, FakeGoogle(), sampling_mode='novelty')
    before = {r['creative_id']: r['headline'] for r in read_rows(old)}
    result = run_scrape(tmp_path, FakeGoogle(), sampling_mode='stratified', max_ads=10)
    assert not result.failed and not result.errors
    rows = read_rows(result)
    assert {r['creative_id']: r['headline'] for r in rows} == before
    assert any(r['sample_role'] == 'exploratory' for r in rows)
    assert all(r['sample_weight'] == '' for r in rows if r['sample_role'] == 'exploratory')


def test_failures_do_not_change_selected_cohort(tmp_path):
    fake = FakeGoogle()
    fake.content_status = 500
    first = run_scrape(tmp_path, fake, sampling_mode='stratified', max_ads=10)
    cohort = summary_of(first)['sampling']['cohort']
    assert first.failed
    fake.content_status = 200
    second = run_scrape(tmp_path, fake, sampling_mode='stratified', max_ads=10)
    assert not second.failed
    assert summary_of(second)['sampling']['cohort'] == cohort


def test_variation_weights_use_creative_selection(tmp_path):
    result = run_scrape(tmp_path, FakeGoogle(), sampling_mode='stratified', max_ads=10, variations=True)
    assert not result.failed and not result.errors
    rows = read_rows(result)
    selected = [r for r in rows if r['sample_role'] == 'representative']
    assert abs(sum(float(r['sample_weight']) for r in selected if r['variation']=='1') - 40) < 1e-6
    for row in selected:
        assert row['sample_weight'] == next(r['sample_weight'] for r in selected if r['creative_id']==row['creative_id'] and r['variation']=='1')
