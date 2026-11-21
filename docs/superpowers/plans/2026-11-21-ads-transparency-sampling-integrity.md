# Ads Transparency: Sampling, Data Integrity and Ads Accounts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ads-transparency` finish large Advertisers in hours by reading a date-covering, novelty-driven Copy sample. Make every CSV row say what happened to its copy. Expose Ads accounts and Ad groups.

**Architecture:** New pure module `sampling.py` (Message normalisation, Coverage, launch-week buckets, allocation). Listed Creatives are admitted to reading at random (deterministic hash) or parked in a `held` queue stage. A `sample` stage moves held Creatives into reading in rounds until Coverage reaches the target (ADR 0003). Export adds `copy_status`, `sample_weight`, `ads_account_id`, `ads_account_source` and `ad_group_id`, and writes `summary.json`. Broken Previews are detected before Codex and retried from another Variation. Image downloads use adaptive pacing in `common/http.py`.

**Tech Stack:** Python 3.13, `uv`, pytest, requests, SQLite `WorkQueue` (`common/pipeline.py`).

**Spec:** `CONTEXT.md` (terms: Inventory, Message, Copy sample, Coverage, Copy status, Broken Preview, Incomplete run, Ads account, Ad group) and `docs/adr/0003-sampled-runs-overlap-with-random-head-start.md`. Exact values are under Global Constraints.

## Global Constraints

- Full read when Google's count estimate ≤ `FULL_READ_LIMIT = 2000`, or the estimate is missing; otherwise sample. `--all` forces a full read.
- Coverage target default `0.95` (`--coverage`, 0 < x < 1); optional `--max-ads N` (N ≥ 1) caps the Creatives read.
- Head start: admission probability `p = HEAD_START / estimate` with `HEAD_START = 2000`. Admission is deterministic per Creative: admitted iff `int(sha1(creative_id).hexdigest()[:8], 16) / 0xFFFFFFFF < p`. `p` is stored once in queue meta `sampling` and never recomputed on reruns.
- Buckets are launch period × running/stopped:
  - Launch period: the ISO week (`"2026-W45"`) when `first_shown ≥ start`, otherwise the month (`"2026-10"`). When `start` is None, always the month.
  - Running vs stopped: `running` iff `last_shown ≥ (end or today) − 2 days` (`RUNNING_SLACK_DAYS = 2`), otherwise `stopped`.
- Minimum `MIN_PER_BUCKET = 3` Creatives read per bucket, or all of the bucket if it has fewer.
- Message = `normalize_message(headline, description)`. Coverage is Good–Turing: `1 − singletons / n` over non-empty Messages of reads with confidence ≥ `MIN_CONFIDENCE` (0.6, already in `copy.py`) or confidence None from `content_js`.
- `copy_status` ∈ `read, not_sampled, pending, no_copy, preview_broken, unreadable` (exact strings).
- New CSV columns, appended after `extraction_confidence` in this order: `copy_status, sample_weight, ads_account_id, ads_account_source, ad_group_id`. `ads_account_source` ∈ `exposed, inferred, ""`.
- A Broken Preview's OCR or Codex text contains "that's an error" (after normalising curly apostrophes) and "please try again later" (case-insensitive).
- Adaptive pacing applies only to `tpc.googlesyndication.com`: start 1.0 s, floor 0.2 s, ceiling 4.0 s. Speed up ×0.9 after 20 clean responses in a row; slow down ×2 on 429, 5xx or a block. Fetch concurrency rises 2 → 4. The RPC host and the content.js host keep their fixed intervals.
- An Incomplete run (listing not finished) still writes `ads.csv` and `summary.json` with `listing_complete: false`. The last log line starts `INCOMPLETE:`. The CLI exit code is 3 when it would otherwise be 0.
- Run all tests with `UV_PROJECT_ENVIRONMENT=venv uv run pytest -q`; all 214 existing tests must keep passing.
- Domain words come from `CONTEXT.md` (Creative, Variation, Preview, Inventory, Message, Ads account …) in code, comments and log messages.

## Review Focus

1. **A rerun on a run directory created before this change.** Its queue has no `held`/`sample` items and no `sampling` meta. It must export with the new columns, deriving `copy_status` from old results, with no crash. Test in Task 1.
2. **Google returns no count estimate.** Every Creative is admitted (full read), and nothing is held forever. Test in Task 7.
3. **The detail RPC is blocked while recovering a Broken Preview.** The extract stage must not crash. The row becomes `preview_broken` with `broken_unverified: true`, and is retried on the next run. Test in Task 3.
4. **`--variations` together with sampling.** Admission happens per Creative, and an admitted Creative goes to `detail`. Its Variations share the Creative's `copy_status` bucket and weight. Test in Task 7.
5. **Rerunning a sampled run with a higher `--coverage`.** The run continues with more rounds, keeps the stored `p`, and does not re-admit or re-read anything. Test in Task 7.

---

## Execution waves

- **Wave 1 (parallel, separate worktrees):** Task 1, Task 3, Task 4, Task 5, Task 6.
- **Wave 2:** Task 2 (needs Tasks 1 and 5).
- **Wave 3:** Task 7 (needs Tasks 1, 2 and 6).

Merge each wave into `feat/ads-transparency-sampling-integrity` and run the full suite before starting the next.

---

### Task 1: Copy status and Ads account / Ad group columns

**Files:**
- Modify: `src/adrenalin/tools/ads_transparency/rpc.py` (add `preview_ids`)
- Modify: `src/adrenalin/tools/ads_transparency/api.py` (`CSV_FIELDS`, `_rows`)
- Modify: `src/adrenalin/tools/ads_transparency/stages.py` (`extract`: drop the per-item "no ad copy in content.js" warning, since status covers it)
- Test: `tests/tools/ads_transparency/test_rpc.py`, `tests/tools/ads_transparency/test_api.py`

**Interfaces:**
- Produces:
  - `rpc.preview_ids(url: str) -> tuple[str, str]` returns `(ads_account_id, ad_group_id)` from the `obfuscatedCustomerId` and `adGroupId` query parameters, `""` when absent.
  - `api.copy_status(extract_status: str | None, result: dict | None, held: bool = False) -> str`.
  - `api.infer_accounts(rows: list[dict]) -> None` fills `ads_account_id` and `ads_account_source` in place.
  - The `_rows` dicts carry the five new keys. `sample_weight` is `1.0` for processed rows (status not in `not_sampled`, `pending`) and `""` otherwise. Task 7 replaces the weight.

- [ ] **Step 1: Write failing tests**
  - `test_preview_ids_reads_account_and_ad_group`: the `content.js` URL from the Rio CSV with `obfuscatedCustomerId=6119286040…adGroupId=196658869119` gives `("6119286040", "196658869119")`; a `simgad` URL gives `("", "")`.
  - `test_copy_status_rules` (table-driven):
    - `held=True` → `not_sampled`.
    - extract status None, `pending` or `failed` → `pending`.
    - Result `preview_broken` → `preview_broken`.
    - Result `content_unparsed` → `unreadable`.
    - Result with any of brand, headline, description or display_url → `read`.
    - Done, no copy, `preview_kind == "content_js"` → `no_copy`.
    - Done, no copy, image with empty `full_text` and format `image` → `no_copy`.
    - Done, no copy, format `text` → `unreadable`.
  - `test_infer_accounts_only_when_website_maps_to_one_account`:
    - Rows: (a.com, exposed X), (a.com, none), (b.com, exposed X), (b.com, exposed Y), (b.com, none).
    - The a.com unknown row becomes `X/inferred`, and the b.com unknown row stays blank.
    - Exposed rows have source `exposed`, and `ad_group_id` is never inferred.
  - `test_scrape_rows_have_status_and_account_columns`: the FakeGoogle scrape gives every row a `copy_status` from the allowed set, and the header ends with the five new columns in order.
  - `test_export_of_pre_change_run_dir`: build a run with the current code path, delete the new keys from stored extract results (simulate the old schema), then `--stage export`. Rows get `copy_status` and there is no exception.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/tools/ads_transparency -q -k "preview_ids or copy_status or infer_accounts or status_and_account or pre_change"`. Expect FAIL.
- [ ] **Step 3:** Implement `preview_ids`, `copy_status`, `infer_accounts` and the `_rows`/`CSV_FIELDS` changes. Read the queue's extract status via `q.items("extract")` (keyed by item key).
- [ ] **Step 4:** Run the full suite and expect PASS. Update existing tests that asserted the removed "no ad copy" warning so they assert `copy_status == "no_copy"` instead.
- [ ] **Step 5:** Commit: `feat(ads-transparency): copy_status and Ads account / Ad group columns`.

---

### Task 2: Run summary and Incomplete run reporting

**Files:**
- Modify: `src/adrenalin/tools/ads_transparency/api.py` (`ScrapeResult`, `scrape`, `_log_summary`, new `build_summary`)
- Modify: `src/adrenalin/tools/ads_transparency/__init__.py` (`run`: exit code 3, `INCOMPLETE:` line)
- Test: `tests/tools/ads_transparency/test_api.py`, `tests/tools/ads_transparency/test_cli.py`

**Interfaces:**
- Consumes: Task 1's `copy_status` and the row keys; Task 5's `RunResult.stage_seconds: dict[str, dict[str, float]]`.
- Produces:
  - `ScrapeResult` gains `listing_complete: bool`, `listed: int`, `estimate: int | None`, `coverage: float | None`, `summary_path: Path`.
  - `api.build_summary(rows: list[dict], q: WorkQueue, rr: RunResult) -> dict` returns these keys:
    - `listing_complete`, `listed`, `google_estimate`
    - `copy_status` ({status: count})
    - `coverage` (None until Task 7)
    - `sampling` (None until Task 7)
    - `buckets` ([] until Task 7)
    - `ads_accounts` (list of `{ads_account_id, source, websites: {site: n}, formats: {fmt: n}}`, sorted by row count descending)
    - `stage_seconds`
  - Writes `summary.json` beside `ads.csv` (reuse `_write_json_atomic`).

- [ ] **Step 1: Write failing tests**
  - `test_summary_written_with_status_counts_and_accounts`: a full FakeGoogle run gives `summary.json` with `listing_complete is True`, `listed == res.rows`, and `copy_status` counts that sum to the row count.
  - `test_block_mid_list_marks_incomplete`: reuse the `two_pages` + sorry setup from `test_block_mid_list_exports_partial_and_reports`. Expect `res.listing_complete is False`, `summary["listing_complete"] is False`, and a log line starting `INCOMPLETE: listed 20 of ~` (capture `log`).
  - `test_cli_stage_export_of_incomplete_run_exits_3` in `test_cli.py`: monkeypatch `api.scrape` to return a `ScrapeResult` with `listing_complete=False`, no failures and no block. Expect exit code 3 and stderr containing `INCOMPLETE`.
- [ ] **Step 2:** Run them and expect FAIL.
- [ ] **Step 3:** Implement. `listing_complete = bool(q.get_meta("list_done"))`, and the estimate comes from `count_estimate` meta. The `INCOMPLETE:` line reads `INCOMPLETE: listed {listed:,} of ~{estimate:,} Creatives; rerun the same command to continue` (without `of ~…` when the estimate is unknown).
- [ ] **Step 4:** Run the full suite and expect PASS.
- [ ] **Step 5:** Commit: `feat(ads-transparency): summary.json and loud Incomplete runs`.

---

### Task 3: Broken Preview detection and Variation fallback

**Files:**
- Modify: `src/adrenalin/tools/ads_transparency/copy.py` (`is_broken_preview`, `AdCopy.preview_broken`, `extract_image`)
- Modify: `src/adrenalin/tools/ads_transparency/stages.py` (`extract` fallback)
- Modify: `src/adrenalin/tools/ads_transparency/api.py` (`_prepare_queue`: requeue done extract items whose stored `full_text` or `headline` is a Broken Preview, and items with `broken_unverified`)
- Create: `tests/fixtures/ads_transparency/broken_preview.png` (copy of `out/ads-transparency/lascade-llc/images/4451538901287659972.png`)
- Test: `tests/tools/ads_transparency/test_copy.py`, `tests/tools/ads_transparency/test_api.py`

**Interfaces:**
- Produces:
  - `copy.is_broken_preview(text: str) -> bool`.
  - `AdCopy.preview_broken: bool = False`.
  - Extract results may carry `preview_broken: bool`, `broken_unverified: bool` and `variation_used: int`, which Task 1's `copy_status` reads as `preview_broken`.

- [ ] **Step 1: Write failing tests**
  - `test_is_broken_preview`: true for `"500. That’s an error. | There was an error. Please try again later."` (curly apostrophe) and the straight-apostrophe form; false for `"$49 Flights"` and for "That's an error" without "please try again later".
  - `test_broken_ocr_skips_codex`: `extract_image` with a fake OCR returning the error text and a `FakeCodexRun` returns `preview_broken=True` with copy fields blank, and Codex `calls == 0`.
  - `test_broken_preview_recovered_from_other_variation` (test_api): make FakeGoogle's first `simgad` OCR text the error page (the fake OCR switches on the image path name), and `DETAIL` return a second image Variation that reads normally. The row's `copy_status == "read"` and the extract result has `variation_used == 2`.
  - `test_broken_preview_all_variations_broken`: every Variation is broken, so `copy_status == "preview_broken"`.
  - `test_broken_preview_detail_blocked_retries_next_run`: the detail RPC returns a sorry redirect, so the row is `preview_broken` with `broken_unverified` stored, the run does not crash, and the next run (cooldown passed) requeues it.
- [ ] **Step 2:** Run and expect FAIL.
- [ ] **Step 3:** Implement.
  - In `extract_image`, check `is_broken_preview(r.text)` right after OCR and before `needs_vision`. Also check the Codex result's headline + full_text.
  - In `AdsStages.extract`, on `preview_broken` for an image Preview, call `ctx.client.variations(advertiser_id, creative_id)`. For each image Preview whose `image_name` differs, download it to `entity/images/` with `write_atomic`, then run `extract_image`. Use the first one that isn't broken and set `variation_used = index + 1`.
  - Catch `Blocked` and `requests.RequestException` around the fallback and set `broken_unverified=True`.
- [ ] **Step 4:** Run the full suite and expect PASS.
- [ ] **Step 5:** Commit: `fix(ads-transparency): detect Broken Previews before Codex, recover from another Variation`.

---

### Task 4: content.js parser fixes (store badges, unparsed video renderer)

**Files:**
- Modify: `src/adrenalin/tools/ads_transparency/copy.py` (`_video_headline_description`, `parse_content_js`)
- Create: `tests/fixtures/ads_transparency/content_video_renderer2.js` (copy of one of the six kept files in `out/ads-transparency/lascade-llc/runs/anywhere_2026-10-20_2026-11-18/content/`)
- Test: `tests/tools/ads_transparency/test_copy.py`

**Interfaces:**
- Produces: no new names. `parse_content_js` returns an `AdCopy` (not None) for the new renderer, so Task 1's `copy_status` gives `read` or `no_copy` instead of `unreadable`.

- [ ] **Step 1: Investigate.** Open the six kept files and find where they carry the video ID (e.g. `ytimg.com/vi/<id>`, `"videoId"`, `youtube.com/embed/<id>`) and any headline, description or app name. Record what you find in a docstring on the new parsing branch.
- [ ] **Step 2: Write failing tests**
  - `test_store_badge_is_not_a_headline`: markup strings `["Hotels70", "Google Play", "Cheap hotel deals tonight", "Install"]` → headline is not `"Google Play"`. Also `"App Store"`.
  - `test_video_renderer2_parsed`: the new fixture parses to non-None, with the `video_id` found in Step 1 (assert the exact ID).
  - Run all six kept files in a loop as a local check (not committed). None may return None.
- [ ] **Step 3:** Run and expect FAIL.
- [ ] **Step 4:** Implement. Add `_STORE_NAMES = {"Google Play", "App Store"}`, skipped like the brand in `_video_headline_description`, and add the new renderer branch.
- [ ] **Step 5:** Run the full suite and expect PASS. Commit: `fix(ads-transparency): store badges are not headlines; parse second video renderer`.

---

### Task 5: Adaptive image pacing and stage timing

**Files:**
- Modify: `src/adrenalin/common/http.py` (`AdaptivePace`, `PacedSession(adaptive=…)`)
- Modify: `src/adrenalin/common/pipeline.py` (`RunResult.stage_seconds`, a `clock` param on `Pipeline`)
- Modify: `src/adrenalin/tools/ads_transparency/api.py` (`make_session` passes `adaptive={IMAGE_HOST: AdaptivePace(1.0, 0.2, 4.0)}`)
- Modify: `src/adrenalin/tools/ads_transparency/stages.py` (`_STAGE_CONFIG["fetch"]` concurrency 4)
- Test: `tests/common/test_http.py`, `tests/common/test_pipeline.py`

**Interfaces:**
- Produces:
  - `http.AdaptivePace(start: float, floor: float, ceiling: float, speedup: float = 0.9, slowdown: float = 2.0, clean_streak: int = 20)` is a frozen dataclass.
  - `PacedSession(..., adaptive: Mapping[str, AdaptivePace] = {})`, plus `PacedSession.interval(host: str) -> float` (the current base interval, for tests and the summary). `derive()` carries the `adaptive` config with fresh state.
  - `RunResult.stage_seconds: dict[str, dict[str, float]]` has keys `busy` (summed worker time across threads) and `waiting` (throttled or idle polling time).

- [ ] **Step 1: Write failing tests**
  - `test_adaptive_speeds_up_after_clean_streak`: 20 clean 200s take `interval` from 1.0 to 0.9; 400 clean responses never go below 0.2.
  - `test_adaptive_slows_on_429_and_5xx`: from 0.5, a 503 gives 1.0; repeated errors cap at 4.0.
  - `test_adaptive_slows_on_block`: a sorry redirect doubles the interval (and still raises `Blocked` as today).
  - `test_non_adaptive_hosts_unchanged`: the RPC host keeps its fixed interval under the same responses.
  - `test_stage_seconds_reported` (pipeline): two fake stages with workers sleeping on a fake clock give `busy > 0` for both, and every stage name is present.
- [ ] **Step 2:** Run and expect FAIL.
- [ ] **Step 3:** Implement. The jitter for adaptive hosts is 0.3 × the current interval. 5xx responses are returned to the caller as today (the existing fetch error path handles them); only the pace changes.
- [ ] **Step 4:** Run the full suite and expect PASS.
- [ ] **Step 5:** Commit: `perf(common): adaptive per-host pacing for image downloads; stage busy/waiting time`.

---

### Task 6: Sampling core (pure functions)

**Files:**
- Create: `src/adrenalin/tools/ads_transparency/sampling.py`
- Test: `tests/tools/ads_transparency/test_sampling.py`

**Interfaces:**
- Produces:
  - `FULL_READ_LIMIT = 2000`, `HEAD_START = 2000`, `MIN_PER_BUCKET = 3`, `RUNNING_SLACK_DAYS = 2`.
  - `normalize_message(headline: str, description: str) -> str`: casefold, map curly quotes to straight, keep letters, digits and `% $ € £ ₹`, drop other punctuation, collapse whitespace, join with `" || "`. Returns `""` when both are blank.
  - `coverage(messages: Sequence[str]) -> float | None`: ignores `""`; None when nothing remains.
  - `admit_probability(estimate: int | None, read_all: bool = False) -> float`.
  - `admitted(creative_id: str, p: float) -> bool` uses the hash rule in Global Constraints.
  - `bucket_key(first_shown: date, last_shown: date, start: date | None, end: date | None, today: date) -> str`, in the format `"2026-W45|running"` or `"2026-10|stopped"`.
  - `@dataclass BucketState(inventory: int, read: int, messages: list[str])`.
  - `allocate(buckets: Mapping[str, BucketState], available: Mapping[str, int], batch: int = 50) -> dict[str, int]`.
  - `should_stop(cov: float | None, read: int, target: float, max_ads: int | None, quotas_met: bool) -> bool`.
  - `sample_weight(inventory: int, read: int) -> float`.

- [ ] **Step 1: Write failing tests**
  - `test_normalize_message`: `("70% Off Hotels!", "Book now.")` and `("70% off hotels", "book  now")` are equal; `"That’s"` equals `"That's"`; `("", "")` gives `""`.
  - `test_coverage_good_turing`: `["a","a","b","c"]` → `0.5`; `[]` → None; `""` entries are ignored.
  - `test_admit_probability`: None → 1.0; 1500 → 1.0; 2000 → 1.0; 80000 → 0.025; `read_all=True` with 80000 → 1.0.
  - `test_admitted_is_deterministic_and_proportional`: the same ID always gives the same answer; over 20,000 synthetic IDs at p=0.1 the admitted share is within 0.09–0.11.
  - `test_bucket_key`:
    - With start 2026-10-20, end 2026-11-18, `first_shown` 2026-11-03 and `last_shown` 2026-11-17 → `"2026-W45|running"`.
    - `first_shown` 2025-03-02 with `last_shown` 2026-11-10 → `"2025-03|stopped"`.
    - With start None → a month key.
  - `test_allocate_fills_min_quota_first`: a bucket with inventory 10 and read 0 gets ≥ 3; a bucket with inventory 2 and read 0 gets exactly 2.
  - `test_allocate_prefers_novel_buckets`: a bucket of all-distinct Messages gets more than a bucket of one repeated Message (same `read` and enough `available`).
  - `test_allocate_never_exceeds_available`.
  - `test_should_stop`: False while `quotas_met` is False; True at cov 0.96 with target 0.95; True when `read >= max_ads`; False when cov is None.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/tools/ads_transparency/test_sampling.py -q` and expect FAIL.
- [ ] **Step 3:** Implement. In `allocate`, first fill the minimum quotas. Then hand out the rest in chunks of 25 to the available buckets ranked by `(singletons + 1) / (read + 2)` over each bucket's Messages, recomputing the ranking after each chunk with that chunk counted as reads with unknown Messages (read += chunk).
- [ ] **Step 4:** Run and expect PASS.
- [ ] **Step 5:** Commit: `feat(ads-transparency): sampling core (Messages, Coverage, buckets, allocation)`.

---

### Task 7: Sampled runs wired into the pipeline and CLI

**Files:**
- Modify: `src/adrenalin/tools/ads_transparency/stages.py` (admission in `list_page`, new `sample` stage and worker, `STAGE_NAMES`, `_STAGE_CONFIG`)
- Modify: `src/adrenalin/tools/ads_transparency/api.py` (`ScrapeOptions.read_all/coverage/max_ads`, `_rows` includes `held` items, real `sample_weight`, summary `coverage`/`sampling`/`buckets`, `_log_summary` Coverage line)
- Modify: `src/adrenalin/tools/ads_transparency/__init__.py` (`--all`, `--coverage`, `--max-ads` and validation)
- Modify: `CLAUDE.md` (architecture line for `sampling.py` and the `sample` stage), `README` if it lists flags
- Test: `tests/tools/ads_transparency/test_api.py`, `tests/tools/ads_transparency/test_cli.py`

**Interfaces:**
- Consumes: Task 6 (all names), Task 1 (`copy_status`, row keys), Task 2 (`build_summary` keys `coverage`, `sampling`, `buckets`).
- Produces:
  - Queue stage `held` (items are never claimed by a worker; same payload as `fetch` or `detail` would get).
  - Queue stage `sample` (round items keyed `round:N`).
  - Meta `sampling` = `{"p": float, "target": float, "max_ads": int | null}`, where `p` is written once and target/max_ads are refreshed from options on every run.
  - Summary `buckets` = list of `{bucket, inventory, read, messages, coverage}`.

- [ ] **Step 1: Write failing tests** (the FakeGoogle search page returns `"4"`/`"5"` count fields, and `FULL_READ_LIMIT`/`HEAD_START` are monkeypatched small)
  - `test_small_inventory_reads_everything`: estimate ≤ limit, so no `held` items and every row is processed.
  - `test_large_inventory_samples_and_stops_at_coverage`: estimate above a monkeypatched limit of 10 and `HEAD_START` 5. The result has `not_sampled` rows, every bucket in the summary has `read ≥ min(3, inventory)`, `summary["coverage"]` is reported, and fewer reads than the Inventory happen when Messages repeat (fake OCR returns the same Message).
  - `test_max_ads_caps_reads`.
  - `test_read_all_flag_overrides_sampling`.
  - `test_rerun_with_higher_coverage_continues` (Review Focus 5): the stored `p` is unchanged, no processed row is re-read (FakeGoogle simgad call count only grows by the newly read rows), and the read count grows.
  - `test_missing_estimate_reads_everything` (Review Focus 2): the search page has no `"4"`/`"5"` fields.
  - `test_variations_with_sampling` (Review Focus 4): admitted Creatives go through `detail`, and the Variation rows share their Creative's status.
  - `test_sample_weight_per_bucket`: read rows' weights in a bucket sum to that bucket's Inventory (within 1e-6).
  - `test_cli_rejects_bad_coverage_and_max_ads`: `--coverage 1.5` and `--max-ads 0` cause a parser error.
- [ ] **Step 2:** Run and expect FAIL.
- [ ] **Step 3: Implement**
  - In `list_page`, when the first page has run (estimate known), write meta `sampling.p` if it's absent.
  - Admitted Creatives go to `fetch` (or `detail`); the rest go to `held` with the same payload plus `bucket`.
  - The `sample` stage sits after `list` in `STAGE_NAMES` (`("list", "sample", "detail", "fetch", "script", "extract")`), with concurrency 1, `throttle_on=("detail", "fetch", "script", "extract")` and `max_ahead=0`.
  - Seed it with `round:0` once `list_done` is set (from `list_page` when there is no next cursor), and on reruns when `held` items remain and `should_stop` is False.
  - The worker builds `BucketState`s from the processed rows' extract results (Messages, excluding low confidence) and the held counts, calls `should_stop`, then `allocate`. It moves the chosen held items into `fetch`/`detail`, deleting them from `held` with a new `WorkQueue.move(stage_from: str, stage_to: str, keys: Iterable[str]) -> None` in `common/pipeline.py` (with a test in `tests/common/test_pipeline.py`), and enqueues `round:N+1`.
  - Treat a sampling round that admitted nothing as stop.
- [ ] **Step 4:** Run the full suite and expect PASS. Then run `uv run adrenalin ads-transparency AR04752318142645010433 --start 2026-10-20 --end 2026-11-18 -p` (Lascade, about 700, full read) and check that `summary.json` reports `listing_complete: true` and no `not_sampled` rows.
- [ ] **Step 5:** Commit: `feat(ads-transparency): sampled runs to a Coverage target (--all, --coverage, --max-ads)`.
