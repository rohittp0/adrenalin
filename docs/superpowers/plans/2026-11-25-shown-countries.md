# Shown Countries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every ads-transparency scrape fetches the Shown countries of its weighted Copy-sample Creatives and writes them to `countries.csv`.

**Architecture:** A new `countries` queue stage makes one `GetCreativeById` request per target Creative and stores the response's raw field 17. With `--variations` it reuses the `detail` stage's response instead of requesting again. Targets are queued at run start, when the listing finishes and after each sampling round. At export, `countries.py` parses the raw entries into rows with a `status` and a `sample_weight`.

**Tech Stack:** Python 3.13, `uv`, pytest, the existing `WorkQueue`/`Pipeline` (ADR 0001), `pycountry`.

**Spec:** The design was agreed in conversation on 2026-11-25. Its decisions are copied verbatim into Global Constraints below. The glossary entry is **Shown country** in `CONTEXT.md`, which is already written.

## Global Constraints

- **Which Creatives:**
  - Shown countries are fetched for weighted Copy-sample Creatives only.
  - Stratified mode: the representative sample, `SampleTally.representative`.
  - Novelty mode or `--all`: at most `--max-ads` admitted Creatives (default `sampling.DEFAULT_SAMPLE_SIZE`), chosen by lowest `sha1(creative_id)`, with weights scaled up.
- **On by default:** `ScrapeOptions.countries = True`. The CLI flag `--no-countries` skips both the fetching and `countries.csv`.
- **Per country we keep:**
  - the ISO alpha-2 code, and `geo:<id>` when the geo ID is not a country;
  - `last_shown`;
  - for EU countries, `first_shown`, `impressions_min` and `impressions_max`.
  - The raw field-17 JSON is kept in the queue result, and parsing happens at export.
- **Output:** the only new output is `<run>/countries.csv`. `ads.csv` and `summary.json` do not change.
- **`countries.csv` columns, in this order:** `creative_id, status, country, first_shown, last_shown, impressions_min, impressions_max, sample_weight`.
- **`status` values:**
  - `shown`: one row per Shown country;
  - `none_reported`: fetched, but no countries listed; one row with a blank country;
  - `pending`: not fetched yet, including failures; one row with a blank country.
- **`sample_weight`:**
  - stratified: the Creative's `ads.csv` weight;
  - novelty or `--all`: the copy weight × (sum of copy weights of all processed Creatives ÷ sum of copy weights of the fetched processed Creatives);
  - blank when the Creative has no copy weight.
- **Pending fetches after a run:** log `"{n:,} Creatives still need Shown countries; rerun the same command to continue"`. The CLI prints that line and exits with code 3 unless an earlier check set a code.
- **Existing runs:** rerunning an existing run's command backfills its Shown countries without reading copy again.
- **Field 17 entry keys:**
  - `"1"` geo ID;
  - `"5"` last shown (yyyymmdd);
  - `"7"` first shown, falling back to `"4"`;
  - `"2"` impressions lower bound and `"3"` impressions upper bound, both strings;
  - when `"3"` is present without `"2"`, the lower bound is 0;
  - `"6"` and `"8"` are not parsed.
- **Geo ID to ISO:** `geo_id - 2000` is the ISO 3166-1 numeric code (`common/geo.py`).

## Review Focus

1. A Google block during the `countries` stage. Expected: that stage stops; image and script downloads continue; the run exits with the "blocked until" code; `countries.csv` shows the unfetched Creatives as `pending`; a rerun finishes them. Test: `test_countries_block_leaves_pending_rows` (Task 3).
2. Malformed field-17 entries: a missing geo ID, a non-numeric date, or a non-list value. Expected: those entries are skipped and never fail the item. Test: `test_parse_shown_countries_skips_malformed` (Task 1).
3. Geo IDs that are not countries (e.g. 2999). Expected: the row carries `geo:2999` rather than being dropped. Test: `test_parse_shown_countries_unknown_geo` (Task 1).
4. `--stage export`. Expected: writes `countries.csv` from the queue and makes no request. Test: `test_stage_export_writes_countries_without_requests` (Task 3).
5. An existing run scraped before this feature. Expected: a rerun fetches countries and does not read copy again. Test: `test_rerun_backfills_countries_without_rereading_copy` (Task 2).

---

### Task 1: Parse Shown countries from the detail response

**Files:**
- Modify: `src/adrenalin/common/geo.py`
- Modify: `src/adrenalin/tools/ads_transparency/rpc.py`
- Modify: `CONTEXT.md` (already edited; commit it here)
- Test: `tests/common/test_geo.py`, `tests/tools/ads_transparency/test_rpc.py`

**Interfaces:**
- Produces:
  - `geo_id_to_region(geo_id: int) -> str | None` in `common/geo.py`.
  - `ShownCountry` in `rpc.py`, a frozen dataclass with fields `country: str`, `last_shown: date | None`, `first_shown: date | None`, `impressions_min: int | None`, `impressions_max: int | None`.
  - `parse_shown_countries(raw: object) -> list[ShownCountry]`.
  - `TransparencyClient.detail(advertiser_id: str, creative_id: str) -> dict`, the raw response. `variations()` becomes `parse_detail(self.detail(...))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/common/test_geo.py
def test_geo_id_to_region_round_trips():
    assert geo_id_to_region(2840) == "US"
    assert geo_id_to_region(region_to_geo_id("DE")) == "DE"
    assert geo_id_to_region(2999) is None

# tests/tools/ads_transparency/test_rpc.py
def test_parse_shown_countries_from_video_fixture():
    raw = json.loads((FIX / "creative_detail_video.json").read_text())["1"]["17"]
    got = parse_shown_countries(raw)
    assert got[0] == ShownCountry("NZ", date(2026, 11, 19), None, None, None)
    de = next(c for c in got if c.country == "DE")
    assert (de.first_shown, de.impressions_min, de.impressions_max) == (date(2026, 3, 22), 0, 1000)

def test_parse_shown_countries_reads_impression_range():
    [c] = parse_shown_countries([{"1": 2276, "2": "10000", "3": "15000", "4": 20250421, "5": 20261122, "7": 20250421}])
    assert (c.impressions_min, c.impressions_max, c.first_shown) == (10000, 15000, date(2025, 4, 21))

def test_parse_shown_countries_first_shown_falls_back_to_4():
    [c] = parse_shown_countries([{"1": 2250, "4": 20260219, "5": 20261122}])
    assert c.first_shown == date(2026, 2, 19)

def test_parse_shown_countries_unknown_geo():
    assert parse_shown_countries([{"1": 2999, "5": 20261122}])[0].country == "geo:2999"

def test_parse_shown_countries_skips_malformed():
    assert parse_shown_countries(None) == [] and parse_shown_countries({"1": 2840}) == []
    got = parse_shown_countries([{"5": 20261122}, {"1": 2840, "5": "soon"}, {"1": 2826, "5": 20261101}])
    assert [(c.country, c.last_shown) for c in got] == [("US", None), ("GB", date(2026, 11, 1))]

def test_client_detail_returns_raw_response_and_variations_still_parse():
    ...  # FakeGoogle-style transport: detail() returns the fixture dict; variations() returns the same previews as before
```

- [ ] **Step 2: Run them and confirm they fail.** Run `uv run pytest tests/common/test_geo.py tests/tools/ads_transparency/test_rpc.py -q`. Expected: an ImportError for `geo_id_to_region` and for `parse_shown_countries`.

- [ ] **Step 3: Implement.**
  - `geo_id_to_region`: use `pycountry.countries.get(numeric=f"{geo_id - 2000:03d}")` and return `alpha_2`. Return None when the ID is not between 2001 and 2999 or the code is unknown.
  - `parse_shown_countries`: a date that won't parse becomes None, but the entry is kept. An entry without an integer `"1"` is skipped. A non-list `raw` returns `[]`. Impressions go through `_int_or_none`.

- [ ] **Step 4: Run them and confirm they pass.** Run `uv run pytest -q`. Expected: everything passes.

- [ ] **Step 5: Commit.** `git commit -m "feat(ads-transparency): parse Shown countries from creative detail"`. Stage `CONTEXT.md` with it.

### Task 2: The `countries` stage, its targets and `--no-countries`

**Files:**
- Create: `src/adrenalin/tools/ads_transparency/countries.py`
- Modify:
  - `src/adrenalin/tools/ads_transparency/stages.py`: `STAGE_NAMES`, `_STAGE_CONFIG`, `StageContext`, `stages()`, `detail()`, `sample()`, `list_page()`, and a new `countries()` worker
  - `src/adrenalin/tools/ads_transparency/api.py`: `ScrapeOptions.countries`, the call at run start, `StageContext` wiring
  - `src/adrenalin/tools/ads_transparency/__init__.py`: `--no-countries`
  - `CLAUDE.md`: list `countries.py` in the `ads-transparency` architecture line
- Test:
  - `tests/tools/ads_transparency/test_countries.py` (new)
  - `tests/tools/ads_transparency/test_api.py`: `run_scrape` sets `countries=False` by default, the same way it sets `sampling_mode`
  - `tests/tools/ads_transparency/test_cli.py`

**Interfaces:**
- Consumes: `TransparencyClient.detail` (Task 1).
- Produces:
  - `countries.COUNTRIES = "countries"`, the stage name.
  - `countries.country_targets(tally: SampleTally, admitted: set[str], cap: int) -> list[str]`, returning Creative IDs:
    - stratified (`tally.representative is not None`): `sorted(tally.representative)`;
    - otherwise: the `cap` admitted IDs with the lowest `sha1(id)`.
  - `countries.queue_countries(q: WorkQueue, variations: bool, query: Query, today: date, max_ads: int | None) -> int`:
    - `admitted` is the Creatives in `listing_stage(variations)`;
    - it puts `q.put(COUNTRIES, cr, {"creative_id": cr})` for each target and returns how many were new.
  - The queue result of a `countries` item is `{"shown_countries": <raw field 17 or None>}`.
  - The `detail` stage result gains the same `"shown_countries"` key.

- [ ] **Step 1: Write the failing tests** (in `test_countries.py`, reusing `FakeGoogle`, `run_scrape` and `summary_of` from `test_api`):

```python
def test_countries_fetched_for_representative_sample(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    selected = summary_of(res)["sampling"]["cohort"]["selected"]
    assert fake.calls["detail"] == len(selected)
    # every countries item is done: open the run's WorkQueue and check counts

def test_countries_reuse_detail_response_with_variations(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, variations=True, countries=True)
    # detail calls == number of detail items (no second request per Creative)

def test_no_countries_makes_no_detail_requests(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=False)
    assert fake.calls["detail"] == 0

def test_novelty_countries_capped_at_max_ads(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="novelty", max_ads=5, countries=True)
    assert fake.calls["detail"] == 5

def test_rerun_backfills_countries_without_rereading_copy(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=False)
    before = dict(fake.calls)
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    assert fake.calls["detail"] - before.get("detail", 0) == 10
    assert fake.calls["content_js"] == before.get("content_js", 0) and fake.calls["simgad"] == before.get("simgad", 0)

def test_country_targets_novelty_is_lowest_hash_capped():
    ...  # tally with representative=None; admitted {"CR1".."CR9"}, cap 3 -> the 3 IDs with lowest sha1, sorted by hash
```

In `test_cli.py`, add `--no-countries` → `ScrapeOptions.countries is False`, and a default run → `True`.

- [ ] **Step 2: Run them and confirm they fail.** Run `uv run pytest tests/tools/ads_transparency/test_countries.py tests/tools/ads_transparency/test_cli.py -q`. Expected: they fail on the unknown `countries` option or module.

- [ ] **Step 3: Implement.**
  - **Stage order and config:**
    - `STAGE_NAMES = ("list", "sample", "detail", "countries", "fetch", "script", "extract")`. `countries` must come after `list` and `sample`, which produce its items.
    - `_STAGE_CONFIG["countries"] = (1, (), 0)`: one worker, and it throttles on nothing, so it never holds back the listing or reading.
  - **When the stage runs:** `stages()` includes it only when `ctx.countries` is True and `only in (None, "list")`.
  - **The worker:**
    - With variations on, if the `detail` item for the Creative is done and its result has `"shown_countries"`, return that.
    - Otherwise call `client.detail(...)`. Raise `ValueError` when `data.get("1")` is not a dict, so the item is retried.
    - Return `{"shown_countries": data["1"].get("17")}`.
  - **Calling `queue_countries`** (only when `ctx.countries`):
    - from `list_page` on the last page, after `seed_round`;
    - from `sample()` after `run_round`;
    - from `api.scrape` before `Pipeline`, when `options.stage in (None, "list")`.

- [ ] **Step 4: Run all tests and confirm they pass.** Run `uv run pytest -q`. Expected: everything passes, and existing tests are unchanged because `run_scrape` now defaults `countries=False`.

- [ ] **Step 5: Commit.** `git commit -m "feat(ads-transparency): fetch Shown countries for the weighted Copy sample"`.

### Task 3: Export `countries.csv` and report pending fetches

**Files:**
- Modify:
  - `src/adrenalin/tools/ads_transparency/countries.py`
  - `src/adrenalin/tools/ads_transparency/api.py`: write the CSV, `ScrapeResult.countries_pending`, the log line, `is_outcome_line`
  - `src/adrenalin/tools/ads_transparency/__init__.py`: print the line and set exit code 3
- Test: `tests/tools/ads_transparency/test_countries.py`, `tests/tools/ads_transparency/test_cli.py`

**Interfaces:**
- Consumes: `parse_shown_countries` (Task 1); `COUNTRIES` and the queue result shape (Task 2); `SampleTally.weight`.
- Produces:
  - `countries.COUNTRY_FIELDS: tuple[str, ...]`, in the column order from Global Constraints.
  - `countries.country_rows(q: WorkQueue, tally: SampleTally, sampled: bool) -> list[dict]`, sorted by `(creative_id, country)`.
  - `countries.countries_message(n: int) -> str`.
  - `ScrapeResult.countries_pending: int = 0`, the number of `countries` items not done.

- [ ] **Step 1: Write the failing tests.**

```python
def test_countries_csv_rows_and_weights(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="stratified", max_ads=10, countries=True)
    rows = list(csv.DictReader((res.csv_path.parent / "countries.csv").open()))
    assert list(rows[0]) == list(COUNTRY_FIELDS)
    assert {r["status"] for r in rows} <= {"shown", "none_reported"} and res.countries_pending == 0
    assert any(r["country"] == "IN" for r in rows)  # DETAIL fixture: geo 2356
    per_creative = {r["creative_id"]: float(r["sample_weight"]) for r in rows}
    assert abs(sum(per_creative.values()) - 40) < 1e-6

def test_novelty_country_weights_match_copy_weight_total(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="novelty", max_ads=5, countries=True)
    # sum of per-Creative country weights == sum of ads.csv sample_weight over variation-1 rows with a weight

def test_none_reported_when_detail_has_no_field_17(tmp_path):
    ...  # fake.detail_reply = DETAIL without "17" -> one row per Creative, status none_reported, blank country

def test_countries_block_leaves_pending_rows(tmp_path):
    fake = FakeGoogle()
    fake.detail_reply = response(302, b"", headers={"Location": "https://www.google.com/sorry/index"})
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    assert res.blocked_until is not None and res.countries_pending == 10
    rows = list(csv.DictReader((res.csv_path.parent / "countries.csv").open()))
    assert {r["status"] for r in rows} == {"pending"} and all(r["country"] == "" for r in rows)

def test_stage_export_writes_countries_without_requests(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True)
    before = fake.calls["detail"]
    res = run_scrape(tmp_path, fake, sampling_mode="stratified", max_ads=10, countries=True, stage="export")
    assert fake.calls["detail"] == before and (res.csv_path.parent / "countries.csv").exists()

def test_no_countries_writes_no_countries_csv(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle(), sampling_mode="stratified", max_ads=10, countries=False)
    assert not (res.csv_path.parent / "countries.csv").exists()
```

In `test_cli.py`, stub `api.scrape` to return a `ScrapeResult` with `countries_pending=3` and nothing else wrong. Expect exit code 3 and stderr containing `3 Creatives still need Shown countries; rerun the same command to continue`.

- [ ] **Step 2: Run them and confirm they fail.** Run `uv run pytest tests/tools/ads_transparency/test_countries.py tests/tools/ads_transparency/test_cli.py -q`.

- [ ] **Step 3: Implement.**
  - **Rows:** `country_rows` maps a done item through `parse_shown_countries`. `pending`, `running` and `failed` items become `pending` rows.
  - **Writing the file:** `api.scrape` writes `countries.csv` with `write_csv_atomic` after `ads.csv`, only when `options.countries` is True.
  - **The pending line:** `_log_summary` logs `countries_message` when there are pending items, and `is_outcome_line` matches it.
  - **The CLI:** it prints the line after the INCOMPLETE check and sets `code = code or 3`.

- [ ] **Step 4: Run all tests and confirm they pass.** Run `uv run pytest -q`. Expected: everything passes.

- [ ] **Step 5: Commit.** `git commit -m "feat(ads-transparency): export countries.csv and report pending Shown countries"`.

- [ ] **Step 6: Live check (manual, after merge).** Rerun the Light Ventures command, which backfills about 2,000 requests: `uv run adrenalin ads-transparency AR04758201045249687553 --start 2026-10-20 --end 2026-11-18 -p`. Expected: `countries.csv` appears next to `ads.csv`. A Google block stops the stage with exit code 2, and rerunning continues it.
