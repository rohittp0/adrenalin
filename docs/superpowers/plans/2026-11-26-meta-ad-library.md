# Meta Ad Library tool and shared network, logging and CLI layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `adrenalin meta-ad-library <page>`, which exports one Facebook Page's Ad Library Creatives to CSV. First make `common/` site-neutral (per-host rules), and give every tool shared logging, progress, CLI flags and exit codes.

**Architecture:** Phase A replaces the Google strings hard-coded in `common/http.py` and `common/playwright_http.py` with a `HostPolicy` per host that each tool declares. Phase B moves logging to stdlib `logging` (a console handler that writes through tqdm, plus a `run.log` per run) and moves the shared CLI flags and exit codes to `common/cli.py`. Phase C builds `tools/meta_ad_library/` on top: a GraphQL client run inside Playwright Chrome, and List → Detail → Fetch → (OCR) → Export stages on the existing `WorkQueue`/`Pipeline`.

**Tech Stack:** Python 3.13, requests, Playwright (installed Chrome), tqdm, SQLite, pytest. No new dependencies.

**Spec:** The design decisions are in `CONTEXT.md` (Meta Ad Library section and the shared terms) and `docs/adr/0004-meta-web-ad-library-not-graph-api.md`. The probe fixtures are in `tests/fixtures/meta_ad_library/`. Read both before starting any Phase C task.

## Global Constraints

- Run everything with `UV_PROJECT_ENVIRONMENT=venv uv run …` (the venv is `venv/`, not `.venv/`).
- `common/` must not contain any Google or Meta host name, cookie name, URL fragment (`/sorry/`, `recaptcha`, `/checkpoint/`) or error code once Phase A is done. Check with `grep -rnE "google|facebook|fbcdn|sorry|recaptcha|checkpoint|GOOGLE_" src/adrenalin/common` → no matches.
- ads-transparency behaviour does not change in Phases A–B: same pacing values, block rules, CAPTCHA handoff, flags and exit codes. All existing offline tests keep passing (they may be rewritten only to use the new constructor arguments or `caplog`).
- CLI tool name: `meta-ad-library`; package: `adrenalin.tools.meta_ad_library`; output: `out/meta-ad-library/<page-slug>/runs/<run>/`.
- Exit codes for every tool: 0 ok, 1 failed items or errors, 2 blocked, 3 incomplete or pending.
- Use the glossary terms in code, columns and messages: Page, Creative, Variation, Card, Display format, Collation group, Template copy, Inactive Creative, EU reach, Inventory, Region, Shown period.
- Live tests are marked `@pytest.mark.live`. No test makes a real request unless it has that mark.
- Commit after each task, ending the message with the attribution lines from the session's system reminder.

## Review Focus

1. **Resuming after a block mid-listing.** The saved cursor is reused, but the `lsd` token belongs to the new browser session. The client must fetch the Library HTML again for a new `lsd` before posting, and a rejected cursor restarts the listing from page 1. Covered by Task 8's `test_resume_refetches_lsd` and `test_expired_cursor_restarts_listing`.
2. **A Page argument that isn't an Ad Library ID.** For a profile URL (`facebook.com/makemytrip`) or a bare slug, the user expects a clear error naming the accepted forms, not an empty run. Covered by Task 9's `test_page_arg_rejects_profile_url`.
3. **A Page with no ads in the Region.** The user expects an empty CSV with a complete listing, exit 0, not an error or "incomplete". Covered by Task 8's `test_empty_page_completes`.
4. **Stale `doc_id`.** Meta returns HTTP 200 with "document … not found". The user expects one clear message that the tool needs updating and exit 1, not a cooldown or N per-item failures. Covered by Task 7's `test_doc_not_found_is_distinct` and Task 8's `test_doc_not_found_stops_run`.
5. **Expired media URLs on a later rerun.** fbcdn answers 403 once the `oe=` deadline passes, and the user expects the rerun to refresh the URLs and finish the images. Covered by Task 8's `test_expired_media_relists`.

---

## Phase A — site-neutral network layer

Tasks 1 → 2 run in order (both edit `http.py`/`playwright_http.py`). **Task 7 has no dependency on Phases A–B and can run in parallel with them.**

### Task 1: `HostPolicy` replaces per-host arguments and the global block check

**Files:**
- Modify: `src/adrenalin/common/http.py`
- Create: `src/adrenalin/tools/ads_transparency/hosts.py`
- Modify: `src/adrenalin/tools/ads_transparency/api.py:60-75,133-139` (pace constants move to `hosts.py`; `make_session` uses `run_session`)
- Test: `tests/common/test_http.py`, `tests/common/test_playwright_transport.py`, `tests/tools/ads_transparency/test_hosts.py` (new)

**Interfaces:**
- Produces (in `common/http.py`):
  - `BlockCheck = Callable[[requests.Response, bool], str | None]`: given (response, read_body), returns a reason when the response is a block, else `None`.
  - `def no_block(resp, read_body) -> str | None` (always `None`).
  - `@dataclass(frozen=True) class Challenge: shown: Callable[[str], bool]; keep_cookies: frozenset[str] = frozenset(); cookie_domains: frozenset[str] = frozenset()`. `shown(url)` says whether a URL is a challenge page. Cookies are persisted only when both name and domain match (domain compared with the leading `.` stripped).
  - `@dataclass(frozen=True) class HostPolicy: pace: AdaptivePace | tuple[float, float] = (0.5, 0.0); browser: bool = False; blocked: BlockCheck = no_block; challenge: Challenge | None = None; entry_path: str = "/"`
  - `PacedSession(cooldown, hosts: Mapping[str, HostPolicy], *, default: HostPolicy = HostPolicy(), proxies=(), wait_on_block=False, max_retries=3, block_seconds=900.0, transport=None, sleep=…, clock=…, rng=…, solve_captcha=False, cookie_dir=None)`. The `intervals`, `default_interval`, `adaptive`, `playwright_hosts` and `log` parameters are removed (`log` goes in Task 4; keep it until then).
  - `PacedSession.policy_key(host: str) -> str`: an exact key wins. Otherwise the longest key beginning with `.` that the host ends with (`.fbcdn.net` matches `scontent-maa5-1.xx.fbcdn.net`). Otherwise the host itself, which gets `default`. **Pacing, adaptive state, cooldown and the `rate:` deadline are all keyed by the policy key**, so every fbcdn subdomain shares one pace and one cooldown.
  - `def run_session(out: Path, hosts: Mapping[str, HostPolicy], *, proxies=(), wait_on_block=False, solve_captcha=False) -> PacedSession`: cooldown at `cache_dir(out)/"cooldown.sqlite3"`, cookie_dir `cache_dir(out)/"captcha"`.
  - `derive()` keeps its current contract and passes `hosts`/`default` through.
- Produces (in `tools/ads_transparency/hosts.py`): `google_block(resp, read_body) -> str | None` (the current `is_block_response` rules, returning e.g. `"/sorry/ redirect"` or `"reCAPTCHA page"`), `GOOGLE_CHALLENGE`, `HOSTS: dict[str, HostPolicy]` with the current values: `HOST` = RPC_INTERVAL, browser, google_block, GOOGLE_CHALLENGE; `SCRIPT_HOST` = SCRIPT_PACE, google_block; `IMAGE_HOST` = IMAGE_PACE, google_block.
- `is_block_response` is deleted from `common/http.py`.

- [ ] **Step 1: Write the failing tests.** In `tests/common/test_http.py`, port each existing `intervals=`/`adaptive=` test to `hosts=` and add:
  - `test_suffix_policy_shares_pace_and_cooldown`: hosts `{".cdn.test": HostPolicy((1.0, 0.0))}`. Block `a.cdn.test` with a policy whose `blocked` returns `"x"`, and assert that a request to `b.cdn.test` raises `Blocked` whose `host == ".cdn.test"`.
  - `test_default_policy_never_blocks`: an undeclared host answering a 302 to `/sorry/` returns the response (no `Blocked`).
  - `test_block_reason_is_logged`: the reason string from `blocked` appears in the warning.

  In `tests/tools/ads_transparency/test_hosts.py`, move the two `is_block_response` assertions from `test_http.py:104-105` to `google_block`.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/common/test_http.py tests/tools/ads_transparency/test_hosts.py -q`. Expected: FAIL (`HostPolicy` not defined).
- [ ] **Step 3:** Implement the interfaces above. `request()` resolves `key = self.policy_key(host)` once and uses `key` everywhere it now uses `host` for pacing, cooldown, `_record` and `Blocked`. It calls `policy.blocked(resp, read_body)` where it now calls `is_block_response`. Route to the Playwright transport when `policy.browser`.
- [ ] **Step 4:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest -q`. Expected: all pass (401 + new).
- [ ] **Step 5:** Commit `refactor(common): per-host HostPolicy instead of hard-coded Google block rules`.

### Task 2: Site-neutral CAPTCHA handoff and exemption cookies in `PlaywrightTransport`

**Files:**
- Modify: `src/adrenalin/common/playwright_http.py`, `src/adrenalin/common/http.py` (pass policies to the transport)
- Modify: `docs/adr/0002-plain-http-over-browser.md` (append a short "Revision: host rules live in the tool" paragraph: the `/sorry/` test, the `GOOGLE_ABUSE_EXEMPTION` name and the domains now live in `tools/ads_transparency/hosts.py`)
- Test: `tests/common/test_playwright_transport.py`

**Interfaces:**
- Consumes: `HostPolicy`, `Challenge` (Task 1).
- Produces: `PlaywrightTransport(*, policies: Callable[[str], HostPolicy], solve_captcha=False, cookie_dir=None)`, where `policies(host)` returns the policy of a host (`PacedSession` passes a lookup based on `policy_key`). Every `"/sorry/" in …` test becomes `policy.challenge is not None and policy.challenge.shown(url)`. `_exemptions(state)` keeps cookies named in `challenge.keep_cookies` whose domain is in `challenge.cookie_domains`, and that haven't expired. The first page of an origin loads `origin + policy.entry_path` instead of the bare origin.
- `GOOGLE_CHALLENGE = Challenge(shown=lambda url: "/sorry/" in url, keep_cookies=frozenset({"GOOGLE_ABUSE_EXEMPTION"}), cookie_domains=frozenset({"google.com", "adstransparency.google.com"}))`.

- [ ] **Step 1: Write the failing tests:**
  - `test_challenge_rule_comes_from_policy`: a fake host whose `Challenge.shown` matches `/blocked/` triggers the handoff path. Mock the page as the existing tests do.
  - `test_only_policy_cookies_persist`: a storage state with `KEEP` (listed), `OTHER` (not listed) and an expired `KEEP` persists only the live `KEEP`.
  - `test_entry_path_loaded_first`: with `entry_path="/ads/library/"`, the first `goto` goes to `https://x.test/ads/library/`.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/common/test_playwright_transport.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the changes. Keep media downloads off the browser path: `browser=False` hosts never reach the transport, and the transport returns `text()`, which would corrupt binary files.
- [ ] **Step 4:** Run the full suite and the grep from Global Constraints. Expected: all pass, and the grep prints nothing.
- [ ] **Step 5:** Commit `refactor(common): CAPTCHA handoff and exemption cookies from the host's Challenge`.

---

## Phase B — shared logging, progress, CLI

Tasks 3 → 4 → 5 → 6 run in order.

### Task 3: `common/runlog.py`: console and per-run log handlers

**Files:**
- Create: `src/adrenalin/common/runlog.py`
- Modify: `src/adrenalin/common/progress.py` (drop `QueueProgress.write`; add `run_progress`)
- Test: `tests/common/test_runlog.py` (new), `tests/common/test_progress.py`

**Interfaces:**
- Produces:
  - `LOGGER = "adrenalin"`. Handlers attach to this logger only, never to the root logger. Set `propagate` to `False` while the console handler is attached, and restore it afterwards.
  - `@contextmanager def console_logging(verbose: bool = False) -> Iterator[None]`: one handler at INFO (DEBUG when verbose) that writes through `tqdm.write(msg, file=sys.stderr)`, so messages print above any active bars. Format: WARNING and above as `"warning: %(message)s"` / `"error: %(message)s"`; INFO and below as the plain `%(message)s` (matches today's output). The handler is removed on exit. Nested use adds no second handler.
  - `@contextmanager def run_log(run_dir: Path) -> Iterator[Path]`: a `FileHandler` on `run_dir/"run.log"` (append mode, DEBUG, format `"%(asctime)s %(levelname)s %(name)s: %(message)s"`), removed and closed on exit. It yields the path. If the logger has no level set, set it to DEBUG for the duration and restore it afterwards.
  - In `progress.py`: `@contextmanager def run_progress(q: WorkQueue, bars: Sequence[Bar], enabled: bool) -> Iterator[QueueProgress | None]` starts and closes `QueueProgress` (does nothing when disabled).

- [ ] **Step 1: Write the failing tests:**
  - `test_console_writes_above_bars`: inside `console_logging()`, `logging.getLogger("adrenalin.x").warning("w")` reaches `tqdm.write` (monkeypatched) as `"warning: w"`.
  - `test_run_log_captures_debug_and_detaches`: after a `with run_log(tmp_path)` block, `run.log` contains a DEBUG line, and `logging.getLogger("adrenalin").handlers` is empty.
  - `test_root_logger_untouched`: `logging.getLogger().handlers` is the same before and after both context managers.
  - `test_nested_run_logs_each_get_own_file`: in nested `run_log(a)` and `run_log(b)`, a message in the inner block lands in both files, and one after the inner block lands only in `a`.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/common/test_runlog.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the interfaces above.
- [ ] **Step 4:** Run the full suite. Expected: all pass.
- [ ] **Step 5:** Commit `feat(common): console and per-run log handlers on the adrenalin logger`.

### Task 4: `common/` modules log through `logging`

**Files:**
- Modify: `src/adrenalin/common/http.py`, `src/adrenalin/common/playwright_http.py`, `src/adrenalin/common/pipeline.py`, `src/adrenalin/common/ocr/codex.py` (if it takes `log`)
- Test: `tests/common/test_http.py`, `tests/common/test_pipeline.py`, `tests/common/test_playwright_transport.py`

**Interfaces:**
- Produces: every `common/` module uses `log = logging.getLogger(__name__)`. The `log=` parameters are removed from `PacedSession`, `PlaywrightTransport`, `HostChecker` and `Pipeline`. `print_to_stderr` is deleted. Level mapping: messages that started with `"warning: "` → `log.warning` with the prefix removed. The pacing-base change → `log.info`. The full block response (`describe_response`) → `log.debug`, with a short `log.warning("%s sent a block page (%s)", key, reason)`.

- [ ] **Step 1:** Rewrite the tests that pass `log=messages.append` to use pytest's `caplog` (`caplog.set_level(logging.DEBUG, logger="adrenalin")`), asserting the same message text without the `"warning: "` prefix and with `record.levelname == "WARNING"` where the old text had it.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/common -q`. Expected: FAIL (parameters still exist, or no records captured).
- [ ] **Step 3:** Migrate the modules as described. In ads-transparency, change only the call sites: drop the `log=` arguments it passes to `PacedSession`, `HostChecker` and `Pipeline`, and nothing else, so the tree stays green. Task 5 migrates the tool's own logging.
- [ ] **Step 4:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `refactor(common): module loggers instead of log callables`.

### Task 5: ads-transparency logs through `logging`; shared output helpers

**Files:**
- Modify: `src/adrenalin/tools/ads_transparency/{api,stages,copy,sample_queue,countries}.py`
- Modify: `src/adrenalin/common/output.py` (add `write_bytes_atomic(path, data)` from `stages.write_atomic`, and `write_json_atomic(path, data)` from `api._write_json_atomic`)
- Create: `src/adrenalin/common/listing.py` with `PaginationLoop(ValueError)`, `page_key(cursor) -> str`, `put_page(q, stage, cursor) -> None`, and `record_cursor(q, item_key, next_cursor) -> None` (raises `PaginationLoop` on a repeated cursor). These move out of `stages.py` (`_seen_cursors`, `page_key`, `_put_page`, the loop check in `list_page`) and `rpc.PaginationLoop`, with behaviour unchanged.
- Test: `tests/tools/ads_transparency/test_api.py`, `tests/common/test_output.py`, `tests/common/test_listing.py` (new: the cursor-loop tests now in `test_api.py`, moved)

**Interfaces:**
- Produces: `scrape(options, *, session=None, ocr=…, codex=None, poll=0.2) -> ScrapeResult` (no `log`). `StageContext.log`, `AdsStages.warn` and `copy.log` become module loggers. `scrape` wraps its body after the run folder is known in `run_log(run)`, and replaces the `emit` closure with `run_progress(...)`. `scrape` logs the outcome lines itself, last, at WARNING, each ending in `"; rerun the same command to continue"` where today's CLI adds that suffix. `is_outcome_line` and `_leading_count` are deleted.

- [ ] **Step 1:** Port the `test_api.py` tests that inspect `log=` output to `caplog`. Add `test_run_log_written`: a fake-session `scrape` leaves `run.log` in the run folder containing the "exported N rows" line.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/tools/ads_transparency -q`. Expected: FAIL.
- [ ] **Step 3:** Migrate the code.
- [ ] **Step 4:** Run the full suite. Expected: all pass.
- [ ] **Step 5:** Commit `refactor(ads-transparency): logging, run.log, shared output and cursor helpers`.

### Task 6: `common/cli.py`: shared flags, Shown period, exit codes

**Files:**
- Create: `src/adrenalin/common/cli.py`
- Modify: `src/adrenalin/tools/ads_transparency/__init__.py`
- Test: `tests/common/test_cli_helpers.py` (new), `tests/tools/ads_transparency/test_cli.py`

**Interfaces:**
- Produces:
  - `add_run_arguments(p)`: `--out` (Path, default `out`), `--proxy` (append, `dest="proxies"`), `--wait-on-block`, `--solve-captcha`, `-p/--progress`, `-v/--verbose`. The help texts are site-neutral ("solve a CAPTCHA or checkpoint in a visible Chrome window").
  - `add_period_arguments(p)`: `--region` (default `anywhere`), `--start`, `--end`, `--days`. `resolve_period(args, parser) -> tuple[date | None, date | None]` holds today's validation (`--days` excludes `--start`/`--end`, `--days >= 1`, start ≤ end, region valid via `region_to_geo_id`).
  - `exit_code(*, failed: Mapping[str, int], errors: Sequence[str], blocked_until: float | None, incomplete: bool) -> int`: 2 if blocked, else 1 if failed or errors, else 3 if incomplete, else 0. This is today's precedence: blocked overrides 1, and 1 overrides 3.
  - `run_cli(args, body: Callable[[], int], *, user_errors: tuple[type[BaseException], ...] = ()) -> int`: opens `console_logging(args.verbose)` and calls `body()`. It maps `Blocked` → logs "blocked until HH:MM; rerun the same command to continue" and returns 2. It maps `ValueError`, `EntityCollision`, `OSError`, `requests.RequestException` and `user_errors` → `log.error(str(e))` and returns 1.
- ads-transparency's `run()` uses all four. Its flags, defaults and exit codes are unchanged apart from the new `-v`.

- [ ] **Step 1:** Write `test_exit_code_precedence` (a table of the four flags → expected code), `test_days_excludes_start` and `test_region_validated`. Keep all of `tests/tools/ads_transparency/test_cli.py` passing unchanged.
- [ ] **Step 2:** Run the new tests. Expected: FAIL.
- [ ] **Step 3:** Implement the helpers and switch ads-transparency over to them.
- [ ] **Step 4:** Run the full suite. Expected: all pass.
- [ ] **Step 5:** Update `CLAUDE.md`'s `common/` list (`runlog.py`, `cli.py`, `listing.py`, `HostPolicy`). Commit `refactor(cli): shared run flags, Shown period and exit codes`.

---

## Phase C — `meta-ad-library`

### Task 7: Parsers for the Library HTML, search pages and detail (can start immediately)

**Files:**
- Create: `src/adrenalin/tools/meta_ad_library/parse.py`
- Test: `tests/tools/meta_ad_library/test_parse.py`; fixtures already in `tests/fixtures/meta_ad_library/`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Card: title: str; body: str; link_url: str; link_description: str; image_url: str; video_url: str`
  - `@dataclass(frozen=True) class Variation: body: str; title: str; link_description: str; caption: str; cta_text: str; link_url: str; image_urls: tuple[str, ...]; video_urls: tuple[str, ...]; video_preview_urls: tuple[str, ...]; cards: tuple[Card, ...]`
  - `@dataclass(frozen=True) class MetaCreative: library_id: str; page_id: str; page_name: str; display_format: str; is_active: bool; start_date: date; end_date: date | None; platforms: tuple[str, ...]; collation_id: str | None; collation_count: int | None; variations: tuple[Variation, ...]`
  - `@dataclass(frozen=True) class SearchPage: creatives: list[MetaCreative]; next_cursor: str | None; skipped: int`
  - `@dataclass(frozen=True) class LibraryPage(SearchPage): lsd: str; count: int | None`. `count` is the Inventory estimate from the HTML.
  - `@dataclass(frozen=True) class EuReach: eu_total: int | None; eu_by_country: dict[str, int]; uk_total: int | None; payer: str; beneficiary: str`
  - `class GraphQLError(ValueError)`, `class DocNotFound(GraphQLError)`.
  - `parse_library_html(html: str) -> LibraryPage`, `parse_search_page(body: str) -> SearchPage`, `parse_detail(body: str) -> EuReach | None` (`None` when `eu_transparency` and `uk_transparency` are both null), `check_errors(body: str) -> None` (raises `DocNotFound` on "GraphQL document with ID … was not found", or `GraphQLError` on any other `errors` without `data`).
- Rules the tests pin:
  - Dates are UTC dates of the epoch seconds (`start_date`/`end_date`).
  - **Template copy:** any field containing `{{` … `}}` becomes `""`.
  - **DCO:** each `snapshot.cards[i]` is one Variation (its `body`, `title`, `link_url`, `cta_text`, `link_description`, `caption`, `original_image_url` or `resized_image_url`, `video_preview_image_url`, `video_hd_url` or `video_sd_url`), and no Cards.
  - **CAROUSEL and DPA:** one Variation built from the top-level snapshot fields, with `cards` = Cards.
  - **IMAGE and VIDEO:** one Variation from the top-level fields, `snapshot.images[]` and `snapshot.videos[]`, and no Cards. When a DCO/CAROUSEL/DPA creative has no cards, fall back to the IMAGE rule.
  - `body` is `snapshot.body.text`.
  - `platforms` are `publisher_platform` in listing order.
  - A malformed result is counted in `skipped`, never raised.
  - `payer` and `beneficiary` come from `aaa_info.payer_beneficiary_data[0]` when present, else `""`.

- [ ] **Step 1: Write the failing tests** (the values come from the fixtures):
  - `test_library_html`: `parse_library_html(library_page.html)` → `count == 921`, `lsd == "TEST_LSD_TOKEN"`, 30 creatives, `next_cursor` not None.
  - `test_inactive_page`: `library_page_inactive.html` → `count == 54`, every `is_active` False, and at least one `display_format == "CAROUSEL"` with `len(variations) == 1` and `len(variations[0].cards) >= 2`.
  - `test_dco_cards_are_variations`: in `search_page.json`, creative `1535930091652737` is DCO with 2 Variations, and both have `title == "Get up to Rs 1500 off* on flight tickets- FLYAPP"`, `cards == ()` and `link_url == "http://play.google.com/store/apps/details?id=com.makemytrip"`.
  - `test_template_copy_dropped`: no Variation anywhere in the three files has `"{{"` in any text field.
  - `test_search_page_cursor`: `parse_search_page(search_page.json)` → 10 creatives, and `next_cursor == "AQHTywakAyxKqjk5cAC3_hIKC1IxXkm3-ry0uvHMTLkOK6LNSeKu7kZBKGf1uYeoVSYh"`.
  - `test_detail_eu`: `parse_detail(detail_eu.json)` → `eu_total == 22`, `eu_by_country["DE"] == 3`, `uk_total == 4`.
  - `test_detail_no_eu`: `parse_detail(detail_no_eu.json) is None`.
  - `test_doc_not_found_is_distinct`: `check_errors(doc_not_found.json)` raises `DocNotFound`. A synthetic `{"errors":[{"message":"x"}]}` raises `GraphQLError` and not `DocNotFound`.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/tools/meta_ad_library/test_parse.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `parse.py`. To find the HTML's embedded connection, locate `"search_results_connection":` and decode the following JSON object with `json.JSONDecoder().raw_decode`. Find `lsd` with the regex `\["LSD",\[\],\{"token":"([^"]+)"`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat(meta-ad-library): parse Library HTML, search pages and EU reach`.

### Task 8: Client, host rules and stages

Depends on Tasks 1–7.

**Files:**
- Create: `src/adrenalin/tools/meta_ad_library/hosts.py`, `client.py`, `stages.py`
- Test: `tests/tools/meta_ad_library/test_client.py`, `test_stages.py`

**Interfaces:**
- `hosts.py`:
  - `LIBRARY_HOST = "www.facebook.com"`, `MEDIA_SUFFIX = ".fbcdn.net"`.
  - `meta_block(resp, read_body) -> str | None`: these responses are blocks:
    - a redirect whose `Location` contains `/login` or `/checkpoint`;
    - a final URL containing either of those;
    - an `/api/graphql/` response whose body doesn't start with `{`;
    - a JSON body whose `errors[*].code` is 1675004 or whose message contains "rate limit" (case-insensitive). This rule is unverified (ADR 0004), so the comment says so.
  - `HOSTS = {LIBRARY_HOST: HostPolicy(AdaptivePace(4.0, 2.5, 30.0, clean_streak=30), browser=True, blocked=meta_block, challenge=Challenge(lambda u: "/checkpoint" in u or "/login" in u), entry_path="/ads/library/"), MEDIA_SUFFIX: HostPolicy(AdaptivePace(1.0, 0.3, 8.0))}`.
- `client.py`:
  - `SEARCH_DOC_ID = "29218106027816007"`, `DETAIL_DOC_ID = "25068828942793558"`, `RELAY_V = "682466"`.
  - `@dataclass(frozen=True) class MetaQuery: page_id: str; country: str  # ISO or "ALL"; status: Literal["all","active","inactive"]; start: date | None; end: date | None`.
  - `class MetaClient(session: PacedSession)`:
    - `library(query) -> LibraryPage`: GET of `https://www.facebook.com/ads/library/?active_status=…&ad_type=all&country=…&search_type=page&view_all_page_id=…&media_type=all`, plus `start_date[min]`/`start_date[max]` when set. It caches the `lsd` on the client.
    - `search(query, cursor: str) -> SearchPage`: POST `/api/graphql/`. The form is `av=0, __user=0, __a=1, lsd, fb_api_caller_class=RelayModern, fb_api_req_friendly_name=AdLibrarySearchPaginationQuery, server_timestamps=true, doc_id, variables`, with headers `x-fb-lsd` and `x-fb-friendly-name`. The variables are exactly the probe's (ADR 0004 / `search_page.json` request): `startDate` = `{"min": "...", "max": "..."}` or `null`, `first = 10`. It calls `library(query)` first if no `lsd` is cached, and runs `check_errors` before parsing.
    - `detail(query, library_id) -> EuReach | None`: `adArchiveID`, `pageID`, `country="ALL"`, `isAdNonPolitical=True`, `isAdNotAAAEligible=False`, `sessionID=None`, `source=None`.
- `stages.py`: `STAGE_NAMES = ("list", "detail", "fetch", "ocr")`. Config is `(concurrency, throttle_on, max_ahead)`:
  - list: (1, ("detail", "fetch", "ocr"), 200)
  - detail: (1, ("fetch",), 50); runs only with `eu_reach`
  - fetch: (4, ("ocr",), 50)
  - ocr: (2, (), 0); runs only with `ocr`
- Stage behaviour:
  - Page `page:` (empty cursor) comes from `client.library`; later pages from `client.search`. The pages use `common.listing`.
  - Each listed Creative is queued under the key `library_id` (or `library_id:n` with `variations`, n = 1-based Variation index; without `variations`, only Variation 1) to `detail` when `eu_reach`, else to `fetch`. The payload is a JSON-able dict of the Creative and its Variation.
  - `fetch` downloads every image URL and video preview URL of the row's Variation and its Cards to `entity/images/<sha1(url without query)[:20]>.jpg` with `write_bytes_atomic`, and skips existing files.
  - `ocr` runs `common.ocr.recognize` on each file and caches the result in `ExtractionCache` under `fbimg:<file stem>`.
  - The `count` is stored as `count_estimate` meta.
  - On `GraphQLError` for a cursor carried over from an earlier run, the listing restarts from page 1 (same rule as ads-transparency's `list_page`).
  - `DocNotFound` is never caught by a stage. `common/pipeline.py` gains `Pipeline(..., fatal: tuple[type[Exception], ...] = ())`: a worker exception of a fatal type releases its item (like `Blocked`), stops every stage, and is appended to `RunResult.errors` as `"<stage>: <Type>: <message>"`. Meta passes `fatal=(DocNotFound,)`. Add `test_fatal_error_stops_all_stages` to `tests/common/test_pipeline.py`.
  - `MetaStages.prepare(q: WorkQueue) -> None` runs before each run's pipeline (not for `--stage export`). If `list_done` is set and any `fetch` item failed with an error containing `HTTP 403` or `HTTP 410`, it requeues `page:` (via `put_page`) and those fetch items, and clears `list_done`. The relist refreshes their payload URLs with `update_payload=True`.
  - Files: also modify `src/adrenalin/common/pipeline.py` and `tests/common/test_pipeline.py`.

- [ ] **Step 1: Write the failing tests** (fake transport returning the fixtures; no network):
  - `test_meta_block_rules`: the four block cases above → a reason; the `search_page.json` response → `None`.
  - `test_search_form`: the captured POST form has `doc_id == SEARCH_DOC_ID`, `lsd == "TEST_LSD_TOKEN"`, and variables with `viewAllPageID`, `countries == ["IN"]` and `activeStatus == "all"`.
  - `test_resume_refetches_lsd`: a new `MetaClient` asked for `search(query, cursor)` first issues one GET of the Library URL.
  - `test_expired_cursor_restarts_listing`: a resumed run whose saved cursor gets a `GraphQLError` puts `page:` back to pending.
  - `test_empty_page_completes`: a Library HTML with `count: 0` and no edges → `list_done` meta set, no fetch items.
  - `test_doc_not_found_stops_run`: the worker raises `DocNotFound` and no item is marked failed (`Pipeline` records it in `errors`).
  - `test_media_on_suffix_policy`: fetch requests to two different `scontent-*.fbcdn.net` hosts share one pacing key `.fbcdn.net`.
  - `test_expired_media_relists`: with the listing done and fetch items failed with HTTP 403, preparing the next run requeues `page:` and the failed fetch items, so the payloads are refreshed with `update_payload=True`.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/tools/meta_ad_library -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the three modules.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat(meta-ad-library): GraphQL client, Meta host rules and queue stages`.

### Task 9: `api.scrape`, CSV export and the CLI

**Files:**
- Create: `src/adrenalin/tools/meta_ad_library/api.py`, `src/adrenalin/tools/meta_ad_library/__init__.py`
- Test: `tests/tools/meta_ad_library/test_api.py`, `test_cli.py`

**Interfaces:**
- `@dataclass(frozen=True) class MetaOptions: page_id: str; region: str = "anywhere"; status: str = "all"; start: date | None = None; end: date | None = None; variations: bool = False; eu_reach: bool = False; ocr: bool = False; wait_on_block: bool = False; solve_captcha: bool = False; stage: str | None = None; out: Path = Path("out"); proxies: tuple[str, ...] = (); progress: bool = False`.
- `@dataclass class MetaResult: csv_path: Path; rows: int; blocked_until: float | None; failed: dict[str, int]; errors: list[str]; listing_complete: bool; listed: int; estimate: int | None; summary_path: Path`.
- `def parse_page_arg(text: str) -> str`: accepts digits, or any URL with `view_all_page_id=<digits>`. Anything else raises `ValueError("expected a numeric Page ID or an Ad Library URL containing view_all_page_id=…")`.
- `def scrape(options: MetaOptions, *, session: PacedSession | None = None, poll: float = 0.2) -> MetaResult`. The entity folder is `entity_dir(out, "meta-ad-library", page_id, page_name)`, with the name from the first listed Creative or the Page's ID when there are none. The run folder is `<region>_<status>_<start|all>_<end|all>[_variations]`. `run_log(run)`, `run_progress(...)`, `Pipeline` and `write_csv_atomic` work as in ads-transparency.
- `CSV_FIELDS = ["library_id", "ad_library_url", "page_id", "page_name", "variation", "display_format", "is_active", "start_date", "end_date", "platforms", "collation_id", "collation_count", "body", "title", "link_description", "caption", "cta_text", "link_url", "card_titles", "card_bodies", "card_links", "image_urls", "image_files", "video_urls", "image_text", "eu_total_reach", "eu_reach_by_country", "uk_total_reach", "payer", "beneficiary"]`.
  - `ad_library_url` = `https://www.facebook.com/ads/library/?id=<library_id>`.
  - List columns are joined with `" | "`. `eu_reach_by_country` is compact JSON.
  - `image_text` is filled only with `--ocr`.
- `summary.json` keys: `listed`, `meta_estimate`, `listing_complete`, `rows`, `inactive_listed`, `stage_seconds`.
- Outcome lines logged last: blocked, INCOMPLETE (same text as ads-transparency, with "~N" from `meta_estimate`), and, when the Region is neither `anywhere` nor in `EU_COUNTRIES` (a frozenset of the 27 EU ISO codes in `api.py`), the INFO note "Inactive Creatives outside the EU are only partly listed by Meta; don't read stopped counts as complete."
- `DocNotFound` → `log.error("Meta changed its Ad Library queries (GraphQL document not found); update SEARCH_DOC_ID / DETAIL_DOC_ID in tools/meta_ad_library/client.py")` and exit 1.
- `__init__.py`: `TOOL = Tool("meta-ad-library", "Export one Facebook Page's Meta Ad Library Creatives to CSV", add_arguments, run)`. The arguments are `page` (positional), `add_period_arguments`, `add_run_arguments`, `--status {all,active,inactive}` (default all), `--variations`, `--eu-reach`, `--ocr` and `--stage {list,fetch,export}`. `run` uses `run_cli`, `resolve_period` and `exit_code`.

- [ ] **Step 1: Write the failing tests:**
  - `test_export_rows`: run with a fake session serving `library_page.html` then a GraphQL page with `has_next_page: false` (derived from `search_page.json`), and fake media bytes. Assert rows = 30 + 10, the columns equal `CSV_FIELDS`, `is_active` values come from the fixture, a DCO row's `title` equals the fixture title, and `listing_complete`.
  - `test_variations_rows`: the same with `variations=True` gives one row per Variation (the DCO creative with 2 Variations gives 2 rows).
  - `test_eu_reach_columns`: with `eu_reach=True` and the detail fixture → `eu_total_reach == "22"`.
  - `test_page_arg_rejects_profile_url`: `parse_page_arg("https://www.facebook.com/makemytrip")` raises `ValueError` with the message above, and `parse_page_arg("https://www.facebook.com/ads/library/?view_all_page_id=120740541030&country=IN") == "120740541030"`.
  - `test_cli_exit_codes`: a blocked run → 2, an incomplete run → 3, a doc-not-found run → 1.
  - `test_tool_discovered`: `discover_tools()` contains `"meta-ad-library"`.
- [ ] **Step 2:** Run `UV_PROJECT_ENVIRONMENT=venv uv run pytest tests/tools/meta_ad_library -q`. Expected: FAIL.
- [ ] **Step 3:** Implement `api.py` and `__init__.py`.
- [ ] **Step 4:** Run the full suite. Expected: all pass.
- [ ] **Step 5:** Commit `feat(meta-ad-library): scrape API, CSV export and CLI`.

### Task 10: Live check, docs

**Files:**
- Create: `tests/tools/meta_ad_library/test_live.py`
- Modify: `README.md`, `CLAUDE.md`, `CONTEXT.md` (only if a probe fact changed)

- [ ] **Step 1:** Write `test_live_makemytrip_in` (`@pytest.mark.live`): `scrape(MetaOptions("120740541030", region="IN", status="active", stage="list", out=tmp_path))` → `listing_complete`, `listed >= 100`, no `errors`. Run it once: `UV_PROJECT_ENVIRONMENT=venv uv run pytest -m live tests/tools/meta_ad_library -v -s`. Expected: PASS, about 90 GraphQL requests at 2.5–4 s each.
- [ ] **Step 2:** In `README.md`, add a `meta-ad-library` usage section: page argument forms, flags, the Inactive Creative caveat, and that EU reach costs one request per Creative. In `CLAUDE.md`, add the tool to "What this is" and the architecture list, and the new `common/` modules if Task 6 missed any.
- [ ] **Step 3:** Run the full offline suite and the `common/` grep. Expected: all pass, and the grep prints nothing.
- [ ] **Step 4:** Commit `docs(meta-ad-library): usage, architecture and live check`.
