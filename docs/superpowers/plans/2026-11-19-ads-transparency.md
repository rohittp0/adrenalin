# ads-transparency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `adrenalin` CLI wrapper, its shared `common` layer, and the first tool, `ads-transparency`, which exports a Google Ads Transparency advertiser's creatives with their ad copy to CSV.

**Architecture:** `adrenalin.cli` finds tools by scanning `adrenalin.tools.*` for a `TOOL` object. The tool runs List → (Detail) → Fetch → Extract as concurrent stages from a SQLite queue, with backpressure (ADR 0001). It calls Google's internal RPCs over paced plain HTTP (ADR 0002). Copy comes from `content.js` parsing, then OCR, then Codex as the fallback.

**Tech Stack:** Python ≥3.13, uv (`uv_build` backend), `requests`, `pycountry`, `Pillow`, `pytesseract`, `ocrmac` (macOS only), stdlib `sqlite3`/`threading`/`argparse`, pytest, and the `codex` CLI as a subprocess.

**Spec:** `docs/superpowers/specs/2026-11-19-ads-transparency-design.md` (read it first). Terms: `CONTEXT.md`. Decisions: `docs/adr/0001-*.md`, `docs/adr/0002-*.md`.

## Global Constraints

- Source lives under `src/adrenalin/`. Tests under `tests/`. Run everything with `uv run …`.
- `main.py` is deleted. `[project.scripts] adrenalin = "adrenalin.cli:main"`.
- `ocrmac` is installed only on macOS: `ocrmac; sys_platform == 'darwin'`.
- Default tests make **no network calls**. Live tests are marked `@pytest.mark.live`, and `pyproject` sets `addopts = "-m 'not live'"`.
- Fixtures are already captured in `tests/fixtures/ads_transparency/`: `search_page.json` (40 creatives, formats 1/2/3, has a cursor), `creative_detail.json` (4 variations), `content_text.js`, `content_image.js`, `content_video.js`, `simgad_text.png`, `simgad_image.png`, `oembed.json`.
- Pacing minimums: `adstransparency.google.com` 6 s ± 2 s, uniform jitter. `tpc.googlesyndication.com`, `displayads-formats.googleusercontent.com` and `www.youtube.com` 0.5 s.
- `429` backoff is `min(60·2**attempt, 900)` s or `Retry-After` if larger, up to 3 retries. A block (`/sorry/` or reCAPTCHA) sets a 900 s cooldown.
- Backpressure limits: List 200 (over detail+fetch+extract), Detail 50 (fetch+extract), Fetch 50 (extract).
- Codex: default model `gpt-5.6-luna` (`$ADRENALIN_VISION_MODEL` overrides), `-c model_reasoning_effort=low`, limit 200 per run, concurrency 2, `stdin=DEVNULL`, run in a temp dir that is deleted afterwards, `--sandbox read-only --skip-git-repo-check`.
- The OCR → vision fallback threshold is mean confidence < 0.6.
- Every temp file or dir a test or the code creates is removed (`tmp_path` or `TemporaryDirectory`).
- Work on branch `feat/ads-transparency` (create it before Task 1; `main` stays untouched). Commit after each task with the attribution trailer from the session instructions.

## Review Focus

1. **Advertiser with zero creatives in the requested window** (for example an old `--days 7` range). Expected: the advertiser name is still resolved by an unfiltered lookup, the CSV is written with a header only, and the tool exits 0. Test in Task 7.
2. **Crash mid-run, then rerun.** Items stuck as `running` must go back to `pending`, nothing is duplicated, and the CSV has each creative once. Test in Task 3 (queue) and Task 7 (CSV deduplication).
3. **Google returns the same cursor twice, or a malformed creative.** A repeated cursor raises `PaginationLoop` (List stops with an error, other stages drain). A malformed creative is skipped with a warning, not a crash. Tests in Task 5.
4. **No OCR engine available** (Linux without tesseract, or `ocrmac` import fails). Expected: image rows are recorded as failed with a clear "install tesseract" message, and the run continues. Test in Task 4.
5. **Codex missing, not logged in, rejecting the model or timing out.** Expected: the row keeps its OCR result, a warning names the error, and the run doesn't fail. Test in Task 4 (`CodexRunner`) and Task 6 (fallback keeps OCR).

---

## File Structure

```
pyproject.toml                       # modify: package, scripts, deps, pytest config
.gitignore                           # modify: add out/, .venv/, __pycache__/
src/adrenalin/__init__.py
src/adrenalin/cli.py                 # Task 1: tool discovery + dispatch
src/adrenalin/common/__init__.py
src/adrenalin/common/tool.py         # Task 1: Tool dataclass
src/adrenalin/common/output.py       # Task 1: slugify, entity_dir, write_csv_atomic, cache_dir
src/adrenalin/common/geo.py          # Task 1: region → geo id
src/adrenalin/common/http.py         # Task 2: Cooldown, PacedSession, Blocked
src/adrenalin/common/pipeline.py     # Task 3: WorkQueue, Stage, Pipeline
src/adrenalin/common/ocr/__init__.py # Task 4: recognize() engine selection
src/adrenalin/common/ocr/base.py     # Task 4: OcrLine, OcrResult, OcrUnavailable
src/adrenalin/common/ocr/apple_vision.py
src/adrenalin/common/ocr/tesseract.py
src/adrenalin/common/ocr/codex.py    # Task 4: CodexRunner
src/adrenalin/common/cache.py        # Task 4: ExtractionCache
src/adrenalin/tools/__init__.py
src/adrenalin/tools/ads_transparency/__init__.py   # Task 7: TOOL + argparse
src/adrenalin/tools/ads_transparency/rpc.py        # Task 5
src/adrenalin/tools/ads_transparency/copy.py       # Task 6: AdCopy, content.js + OCR layout parsing, extract_image()
src/adrenalin/tools/ads_transparency/stages.py     # Task 7: stage workers + CSV rows
src/adrenalin/tools/ads_transparency/api.py        # Task 7: ScrapeOptions, scrape(), list_creatives()
tests/...                            # mirrors src
```

**Parallelism:** Task 1 first. Then Tasks 2, 3 and 4 in parallel. Then Tasks 5 and 6 in parallel. Then 7, then 8. Task 1 creates every shared file the parallel tasks touch (`common/http.py` with `Blocked`, `tools/ads_transparency/__init__.py`), so the parallel tasks never edit the same file.

---

### Task 1: Scaffold, CLI discovery, output and geo helpers

**Files:**
- Modify: `pyproject.toml`, `.gitignore`. Delete: `main.py`
- Create: `src/adrenalin/{__init__,cli}.py`, `src/adrenalin/common/{__init__,tool,output,geo}.py`, `src/adrenalin/common/http.py` (only `class Blocked(Exception)` with `__init__(self, host: str, until: float)`; Task 2 fills in the rest), `src/adrenalin/tools/__init__.py`, an empty `src/adrenalin/tools/ads_transparency/__init__.py`
- Test: `tests/test_cli.py`, `tests/common/test_output.py`, `tests/common/test_geo.py`

**Interfaces:**
- Produces:
  - `Tool(name: str, help: str, add_arguments: Callable[[ArgumentParser], None], run: Callable[[Namespace], int])`, a frozen dataclass.
  - `discover_tools(package: ModuleType = adrenalin.tools) -> dict[str, Tool]`.
  - `main(argv: list[str] | None = None) -> int`.
  - `slugify(name: str) -> str`.
  - `entity_dir(out_root: Path, tool: str, entity_id: str, name: str) -> Path`, which raises `EntityCollision`.
  - `write_csv_atomic(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping]) -> int` (returns the row count).
  - `cache_dir(out_root: Path) -> Path`.
  - `region_to_geo_id(region: str) -> int | None`.

- [ ] **Step 1: Configure the project.** In `pyproject.toml`:
  - Add `[build-system] requires=["uv_build>=0.9"] build-backend="uv_build"`.
  - Add dependencies `requests`, `pycountry`, `Pillow`, `pytesseract`, `ocrmac; sys_platform == 'darwin'`.
  - Add `[dependency-groups] dev=["pytest"]` and `[project.scripts] adrenalin = "adrenalin.cli:main"`.
  - Add `[tool.pytest.ini_options] addopts="-m 'not live'"`, `markers=["live: hits real Google endpoints"]`, `addopts` also gets `--import-mode=importlib` (two `test_cli.py` files exist), `testpaths=["tests"]`.
  - Set the description to "Competitor-analysis tools for marketing teams".

  Append `out/`, `.venv/` and `__pycache__/` to `.gitignore`. Delete `main.py`. Run `uv sync`. Expected: it installs without errors.

- [ ] **Step 2: Write failing tests.**

```python
# tests/test_cli.py
def test_discovers_tool_modules_exposing_TOOL(tmp_path, monkeypatch):
    pkg = tmp_path / "fake_tools"; (pkg / "hello").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "hello" / "__init__.py").write_text(
        "from adrenalin.common.tool import Tool\n"
        "TOOL = Tool('hello', 'says hi', lambda p: p.add_argument('who'), lambda a: print('hi', a.who) or 0)\n")
    (pkg / "not_a_tool.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import fake_tools
    assert set(discover_tools(fake_tools)) == {"hello"}

def test_main_dispatches_and_returns_tool_exit_code(monkeypatch, capsys): ...  # monkeypatch discover_tools → {"hello": Tool(... run returns 3)}; main(["hello","bob"]) == 3; "hi bob" in stdout
def test_main_without_tool_prints_tool_list_and_exits_2(capsys): ...         # SystemExit code 2

# tests/common/test_output.py
def test_slugify(): assert slugify("Lascade LLC") == "lascade-llc"; assert slugify("  Café & Co. ") == "cafe-co"
def test_entity_dir_creates_and_reuses(tmp_path):
    d = entity_dir(tmp_path, "ads-transparency", "AR1", "Lascade LLC")
    assert d == tmp_path / "ads-transparency" / "lascade-llc"
    assert json.loads((d / "entity.json").read_text()) == {"id": "AR1", "name": "Lascade LLC"}
    assert entity_dir(tmp_path, "ads-transparency", "AR1", "Lascade LLC") == d
def test_entity_dir_same_slug_different_id_raises(tmp_path):
    entity_dir(tmp_path, "t", "AR1", "Lascade LLC")
    with pytest.raises(EntityCollision, match="AR1"): entity_dir(tmp_path, "t", "AR2", "Lascade, LLC")
def test_write_csv_atomic_leaves_no_temp_files(tmp_path): ...  # returns 2 rows; only ads.csv in dir; header == fieldnames
def test_write_csv_atomic_keeps_old_file_when_rows_raise(tmp_path): ...  # generator raising mid-way → original content intact, no temp left

# tests/common/test_geo.py
def test_region_to_geo_id():
    assert region_to_geo_id("anywhere") is None
    assert region_to_geo_id("IN") == 2356 and region_to_geo_id("us") == 2840 and region_to_geo_id("ES") == 2724
    with pytest.raises(ValueError, match="XX"): region_to_geo_id("XX")
```

- [ ] **Step 3: Run** `uv run pytest tests/test_cli.py tests/common -v`. Expected: FAIL (ImportError).
- [ ] **Step 4: Implement.**
  - `discover_tools` uses `pkgutil.iter_modules(package.__path__)` and `importlib.import_module`, keeping modules whose `TOOL` is a `Tool`.
  - `main` builds `argparse` subparsers from the discovered tools, sorted by name.
  - `slugify` uses NFKD normalisation to ASCII, lowercases, and collapses runs of characters outside `[a-z0-9]` to `-`.
  - `geo` uses `pycountry.countries.get(alpha_2=…)` and returns `2000 + int(numeric)`.
  - `write_csv_atomic` writes to `tempfile.mkstemp` in the target directory, then `fsync` and `os.replace`, and unlinks the temp file in `finally`.
- [ ] **Step 5: Run** the same command. Expected: PASS. Also `uv run adrenalin --help` exits 0.
- [ ] **Step 6: Commit** `feat: scaffold adrenalin CLI with tool discovery and output helpers`.

---

### Task 2: Paced HTTP session with persisted cooldown (`common/http.py`)

**Files:** Create `src/adrenalin/common/http.py`. Test `tests/common/test_http.py`.

**Interfaces:**
- Consumes: nothing beyond Task 1's package.
- Produces:
  - `Blocked` already exists from Task 1. Keep its signature.
  - `class Cooldown(path: Path)` with `remaining(host: str) -> float`, `until(host) -> float` and `block(host: str, seconds: float) -> float` (returns the new until). It is backed by a SQLite table `cooldown(host PRIMARY KEY, until REAL)` and is thread-safe.
  - `PacedSession(cooldown: Cooldown, intervals: Mapping[str, tuple[float, float]], *, default_interval=(0.5, 0.0), proxies: Sequence[str] = (), wait_on_block=False, max_retries=3, block_seconds=900.0, transport: Callable[..., requests.Response] | None = None, sleep=time.sleep, clock=time.time, rng=random.random)`. `intervals` maps host → (base, jitter), and the delay is `base + uniform(-jitter, +jitter)`.
  - `PacedSession.request(method: str, url: str, **kwargs) -> requests.Response` and `.get` / `.post` shortcuts.
  - `PacedSession.close()`.
  - `is_block_response(resp: requests.Response) -> bool`.
  - `DEFAULT_UA: str`, a recent desktop Chrome UA.

- [ ] **Step 1: Write failing tests.** Use a fake transport that returns queued `requests.Response` objects (built as in `gads.py`'s `test_gads.py` helper), a fake `clock` and a recording `sleep`.

```python
def test_spaces_requests_per_host_independently(): ...
    # intervals={"a.com": (6, 0)}; two GETs to a.com + one to b.com at clock t=0 → sleeps == [6]; b.com not delayed
def test_jitter_within_bounds(): ...                       # rng → 0.0 and 1.0 give delays 4 and 8 for (6, 2)
def test_429_backs_off_with_retry_after_then_succeeds(): ... # replies [429 Retry-After: 120, 200] → sleep includes 120; returns 200
def test_429_exhausting_retries_raises_blocked_and_persists(tmp_path): ...
    # 4×429 with max_retries=3 → Blocked; new Cooldown(same path).remaining(host) > 0
def test_sorry_redirect_blocks_for_900s_without_retry(tmp_path): ...
    # reply 302 Location: https://www.google.com/sorry/index → Blocked(until=clock+900); transport called once
def test_recaptcha_html_counts_as_block(): ...             # 200 text/html body containing "recaptcha"
def test_active_cooldown_raises_before_sending(tmp_path): ... # cooldown.block(host, 60) → request raises Blocked; transport never called
def test_wait_on_block_sleeps_through_cooldown_and_retries(tmp_path): ... # replies [sorry, 200] → sleeps ≥900, returns 200
def test_proxies_rotate_per_request(): ...                 # proxies=["p1","p2"] → transport kwargs proxies alternate p1,p2,p1
```

- [ ] **Step 2: Run** `uv run pytest tests/common/test_http.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Use one `threading.Lock` per host. The lock covers the wait and the timestamp update, but not the network call.
  - Send requests with `allow_redirects=False` so `/sorry/` redirects can be seen.
  - Set `User-Agent: DEFAULT_UA` and `Accept-Language: en-US,en;q=0.9` on the default `requests.Session`.
  - Proxies map `{"http": p, "https": p}` and cycle with `itertools.cycle`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(common): paced HTTP session with persisted per-host cooldown`.

---

### Task 3: SQLite work queue and concurrent pipeline (`common/pipeline.py`)

**Files:** Create `src/adrenalin/common/pipeline.py`. Test `tests/common/test_pipeline.py`.

**Interfaces:**
- Consumes: `Blocked` from Task 2. It catches it by type, so import `adrenalin.common.http.Blocked`.
- Produces:
  - `Item(stage: str, key: str, payload: dict, result: dict | None, status: str, error: str | None)`.
  - `WorkQueue(path: Path, *, max_attempts: int = 3)`. Opening it resets `running` items to `pending`, and `failed` items with `attempts < max_attempts` to `pending`. `claim` increments `attempts`. It uses WAL and one connection guarded by a lock (`check_same_thread=False`). Methods:
    - `put(stage, key, payload: dict, *, update_payload: bool = False) -> bool` (`INSERT OR IGNORE`; True if new; with `update_payload`, an existing item's payload is replaced but its status and result are kept)
    - `claim(stage) -> Item | None`
    - `complete(stage, key, result: dict | None = None)`
    - `fail(stage, key, error: str)`
    - `release(stage, key)`
    - `requeue(stage, keys: Iterable[str])` (back to pending, clearing the result)
    - `backlog(*stages: str) -> int` (pending + running)
    - `items(stage, status: str | None = None) -> list[Item]`
    - `get_meta(key) -> str | None` / `set_meta(key, value: str)`
    - `close()`
  - `Stage(name: str, worker: Callable[[Item, WorkQueue], dict | None], concurrency: int = 1, throttle_on: tuple[str, ...] = (), max_ahead: int = 0)`.
  - `Pipeline(queue: WorkQueue, stages: Sequence[Stage], *, poll: float = 0.2, log: Callable[[str], None] = print_to_stderr)` with `.run() -> RunResult`.
  - `RunResult(blocked: Blocked | None, failed: dict[str, int])`.
- Semantics:
  - A worker's return value is stored as the item result. Workers enqueue downstream items themselves via `queue.put`.
  - A stage's upstream is every stage earlier in the list. A stage finishes when all its upstream stages have finished and `backlog(stage) == 0`.
  - On `Blocked`, the item is released, the stage stops (it counts as finished), and `RunResult.blocked` is set.
  - Any other exception calls `fail` with the message, logs it, and continues.
  - Throttling: before claiming, a stage waits while `backlog(*throttle_on) > max_ahead`, polling every `poll` seconds.

- [ ] **Step 1: Write failing tests.**

```python
def test_put_is_idempotent_and_claim_marks_running(tmp_path): ...
def test_reopen_resets_running_to_pending(tmp_path): ...     # claim, close without complete, reopen → backlog==1, claim returns it
def test_failed_items_retry_on_reopen_until_max_attempts(tmp_path): ... # fail 3 times across 3 reopens → 4th reopen leaves it failed
def test_put_update_payload_keeps_result(tmp_path): ...       # complete with result, put(update_payload=True) → new payload, same result and status
def test_items_flow_through_stages_in_order(tmp_path): ...   # A puts 5 items into B; B completes; RunResult.failed=={}; B has 5 done
def test_backpressure_caps_upstream_lead(tmp_path): ...
    # A (throttle_on=("B",), max_ahead=3) emits 1 item per claim from 20 seeds; B worker sleeps 0.01 and records max backlog("B") seen → ≤ 3 + A.concurrency
def test_blocked_stops_stage_but_downstream_drains(tmp_path): ...
    # A raises Blocked on its 3rd item → result.blocked set; A still has pending items; all items A emitted before are done in B
def test_worker_exception_marks_item_failed_and_continues(tmp_path): ... # failed=={"B": 1}; error text stored
def test_concurrency_runs_workers_in_parallel(tmp_path): ...  # concurrency=2, workers block on a Barrier(2) with timeout → no BrokenBarrierError
```

- [ ] **Step 2: Run** `uv run pytest tests/common/test_pipeline.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** with `threading.Thread` workers (`concurrency` per stage) and a shared `threading.Event` per stage for "finished". `run()` joins all threads.
- [ ] **Step 4: Run.** Expected: PASS, with no thread left alive (`threading.active_count()` back to the baseline).
- [ ] **Step 5: Commit** `feat(common): SQLite work queue and backpressured stage pipeline`.

---

### Task 4: OCR engines, Codex runner and extraction cache (`common/ocr/`, `common/cache.py`)

**Files:** Create `src/adrenalin/common/ocr/{__init__,base,apple_vision,tesseract,codex}.py` and `src/adrenalin/common/cache.py`. Test `tests/common/test_ocr.py`, `tests/common/test_codex.py`, `tests/common/test_cache.py`.

**Interfaces:**
- Produces:
  - `OcrLine(text: str, confidence: float, box: tuple[float, float, float, float])`. The box is (x, y, w, h), normalised 0–1, origin **top-left**.
  - `OcrResult(lines: list[OcrLine], method: str)` with properties `text` (lines joined by `\n`, top-to-bottom) and `confidence` (mean, 0.0 if no lines).
  - `class OcrUnavailable(RuntimeError)`.
  - `apple_vision.recognize(path: Path) -> OcrResult`. It uses `ocrmac.OCR(str(path), recognition_level="accurate").recognize()` and converts the bottom-left `y` to top-left.
  - `tesseract.recognize(path) -> OcrResult`. It uses `pytesseract.image_to_data`, groups by `(block, par, line)`, and maps confidence /100.
  - `ocr.recognize(path: Path) -> OcrResult`. It picks Apple Vision on darwin when `ocrmac` imports, otherwise Tesseract. If neither works it raises `OcrUnavailable("No OCR engine: install tesseract (brew install tesseract / apt install tesseract-ocr)")`.
  - `CodexRunner(model: str = DEFAULT_VISION_MODEL, limit: int = 200, concurrency: int = 2, timeout: float = 300, run=subprocess.run)` with `.extract(image: Path, schema: dict, prompt: str) -> dict | None` (None when the limit is reached; increments `.skipped`). It raises `CodexError` on a non-zero exit, timeout or missing binary. Attributes: `.calls`, `.skipped`, `.model`.
  - `DEFAULT_VISION_MODEL = os.environ.get("ADRENALIN_VISION_MODEL", "gpt-5.6-luna")`.
  - `ExtractionCache(path: Path)` with `get(key: str, method: str, model: str = "") -> dict | None` and `put(key, method, model, fields: dict)`. It's thread-safe and backed by SQLite.

- [ ] **Step 1: Write failing tests.**

```python
# tests/common/test_ocr.py
apple = pytest.importorskip("ocrmac") if sys.platform == "darwin" else None
def test_recognize_reads_text_ad_fixture():          # skip if OcrUnavailable
    r = recognize(FIX / "simgad_text.png")
    assert "Book Cheap Flight Tickets" in r.text and "flights70.com" in r.text and r.confidence > 0.6
    assert r.lines == sorted(r.lines, key=lambda l: l.box[1])         # top-to-bottom
def test_recognize_without_engines_raises_ocr_unavailable(monkeypatch): ... # force both engine imports to fail → OcrUnavailable, message mentions "tesseract"

# tests/common/test_codex.py: fake `run` captures argv/kwargs and writes the -o file
def test_codex_invocation_shape(tmp_path): ...
    # argv contains: "exec","--skip-git-repo-check","--sandbox","read-only","-m","gpt-5.6-luna","-c","model_reasoning_effort=low","-i",str(image),"--output-schema",<file>,"-o",<file>
    # kwargs["stdin"] is subprocess.DEVNULL; kwargs["cwd"] is a temp dir that no longer exists after extract() returns
def test_codex_returns_parsed_json(tmp_path): ...
def test_codex_limit_returns_none_and_counts_skipped(tmp_path): ... # limit=1 → second call None, skipped==1, run called once
def test_codex_failure_raises_codex_error(tmp_path): ...            # returncode 1 with stderr "model is not supported" → CodexError containing that text
def test_codex_concurrency_never_exceeds_limit(tmp_path): ...       # 6 threads, fake run sleeps 0.05 and tracks in-flight max → ≤ 2
@pytest.mark.live
def test_codex_live_extracts_rent80(): ...  # real codex on simgad_image.png → brand == "Rent80"

# tests/common/test_cache.py
def test_cache_roundtrip_and_model_scoping(tmp_path): ... # put(k,"codex","m1",{...}); get(k,"codex","m2") is None
```

- [ ] **Step 2: Run** `uv run pytest tests/common/test_ocr.py tests/common/test_codex.py tests/common/test_cache.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The Codex prompt and schema come from the caller, so this module stays tool-agnostic.
  - Use `threading.BoundedSemaphore(concurrency)` and a lock-protected counter. The limit check happens before acquiring the semaphore.
- [ ] **Step 4: Run.** Expected: PASS. On this Mac the Apple Vision path runs (the 0.6 confidence check is real).
- [ ] **Step 5: Commit** `feat(common): OCR engines, Codex vision runner and extraction cache`.

---

### Task 5: Transparency RPC client and parsers (`tools/ads_transparency/rpc.py`)

**Files:** Create `src/adrenalin/tools/__init__.py` (exists from Task 1), `src/adrenalin/tools/ads_transparency/rpc.py`, and an empty `tools/ads_transparency/__init__.py` placeholder with no `TOOL` yet (Task 7 fills it). Test `tests/tools/ads_transparency/test_rpc.py`.

**Interfaces:**
- Consumes: `PacedSession` (Task 2).
- Produces:
  - Constants: `HOST = "adstransparency.google.com"`, `SEARCH_URL = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives"`, `DETAIL_URL = ".../anji/_/rpc/LookupService/GetCreativeById"`.
  - `Query(advertiser_id: str, geo_id: int | None, start: date | None, end: date | None)`.
  - `Preview(kind: Literal["image", "content_js"], url: str, width: int | None, height: int | None)` with property `image_id -> str | None` (the last path segment of a `simgad` URL).
  - `ListedCreative(creative_id, advertiser_name, format: Literal["text", "image", "video", "unknown"], first_shown: datetime, last_shown: datetime, days_shown: int | None, preview: Preview | None)`.
  - `SearchPage(creatives: list[ListedCreative], next_cursor: str | None, count_low: int | None, count_high: int | None, skipped: int)`.
  - `class PaginationLoop(ValueError)`.
  - `build_search_request(query, cursor: str | None, page_size: int = 100) -> dict`.
  - `parse_preview(obj: dict) -> Preview | None`, `parse_search_page(data: dict) -> SearchPage`, `parse_detail(data: dict) -> list[Preview]` (identical previews collapsed, order kept: same `image_id`, or same `content.js` URL ignoring the `htmlParentId` and `responseCallback` params).
  - `transparency_url(advertiser_id, creative_id, region: str) -> str`.
  - `TransparencyClient(session: PacedSession)` with `.search(query, cursor=None, page_size=100) -> SearchPage`, `.variations(advertiser_id, creative_id) -> list[Preview]` and `.advertiser_name(query) -> str | None` (tries `query`, then an unfiltered query; None if both are empty).
- Request encoding:
  - `{"2": page_size, "3": {"12": {"1": "", "2": True}, "13": {"1": [adv]}, "8": [geo] (only if geo), "6": YYYYMMDD(start), "7": YYYYMMDD(end + 1 day)}, "7": {"1": 1, "2": 0, "3": 2356}, "4": cursor (only if set)}`. The dates are integers. The end date in the request is exclusive, so send the inclusive end plus one day.
  - Detail request: `{"1": adv, "2": cr, "5": {"1": 1, "2": 0, "3": 2356}}`.
  - Both are POSTed as form `f.req=<compact json>`, with `params={"authuser": "0"}` and headers `X-Same-Domain: 1`, `Origin: https://adstransparency.google.com` and `Referer: https://adstransparency.google.com/advertiser/{adv}`.
- Response fields:
  - Each creative: `"2"` is the CR id, `"12"` the advertiser name, `"4"` the format (1 → text, 2 → image, 3 → video), `"6"`/`"7"` are `{"1": epoch_s_str, "2": nanos}` for first/last shown, and `"13"` is days shown.
  - Preview: `"3"."3"."2"` is an `<img src=… height=… width=…>` HTML string; `"3"."1"."4"` is a `content.js` URL.
  - Page: `"2"` is the cursor, `"4"`/`"5"` the count band.
  - Detail: `"1"."5"` is a list of previews.

- [ ] **Step 1: Write failing tests.**

```python
def test_build_search_request_with_region_and_dates():
    q = Query("AR1", 2356, date(2026, 11, 12), date(2026, 11, 18))
    r = build_search_request(q, cursor="abc", page_size=100)
    assert r["3"]["8"] == [2356] and r["3"]["6"] == 20261112 and r["3"]["7"] == 20261119 and r["4"] == "abc"
def test_build_search_request_anywhere_all_time_omits_filters(): ...  # no "8","6","7" in r["3"], no "4"
def test_parse_search_page_fixture():
    p = parse_search_page(json.loads((FIX / "search_page.json").read_text()))
    assert len(p.creatives) == 40 and p.next_cursor and p.skipped == 0
    assert {c.format for c in p.creatives} == {"text", "image", "video"}
    assert all(c.advertiser_name == "Lascade LLC" for c in p.creatives)
    img = next(c for c in p.creatives if c.preview.kind == "image")
    assert img.preview.image_id.isdigit() and img.preview.width and img.preview.url.startswith("https://tpc.googlesyndication.com/archive/simgad/")
def test_parse_search_page_skips_malformed_creative(): ...  # creative missing "2" → skipped==1, others parsed
def test_parse_detail_fixture_dedupes_to_2_variations(): ... # 4 raw entries: 3× image_id 877210798417700731 + 10849098364905980222 → 2 previews in that order
def test_transparency_url(): assert transparency_url("AR1", "CR2", "IN") == "https://adstransparency.google.com/advertiser/AR1/creative/CR2?region=IN"
def test_client_search_posts_form_and_detects_cursor_loop(): ... # fake PacedSession returning fixture twice with same cursor → iter_pages raises PaginationLoop on the repeated cursor
def test_advertiser_name_falls_back_to_unfiltered_query(): ...   # first search returns {"1": []} → second (no geo/dates) returns fixture → "Lascade LLC"
```

- [ ] **Step 2: Run** `uv run pytest tests/tools/ads_transparency/test_rpc.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Add `TransparencyClient.iter_pages(query, start_cursor=None) -> Iterator[SearchPage]`. It tracks seen cursors and raises `PaginationLoop`. This is the API other tools use.
  - `.search` raises `ValueError` on a non-JSON response or one without a `"1"` list. An empty page (`{}`) is a valid end of results.
  - Parse the `<img>` attributes with a regex on `src`, `height` and `width`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(ads-transparency): RPC request builders, parsers and client`.

---

### Task 6: Ad copy extraction (`tools/ads_transparency/copy.py`)

**Files:** Create `src/adrenalin/tools/ads_transparency/copy.py`. Test `tests/tools/ads_transparency/test_copy.py`.

**Interfaces:**
- Consumes: `OcrResult`, `OcrLine`, `recognize`, `CodexRunner`, `CodexError`, `OcrUnavailable` and `ExtractionCache` (Task 4).
- Produces:
  - `AdCopy` dataclass. All string fields default to `""`: `brand`, `display_url`, `website`, `landing_url`, `headline`, `description`, `cta`, `full_text`, `logo_url`, `video_id`, `method`, `model`. Also `sitelinks: list[str]`, `image_urls: list[str]` and `confidence: float | None`. Methods: `to_dict()` and `from_dict()`.
  - `website_from(display_url: str) -> str` (`"www.flights70.com/"` → `"flights70.com"`, `"app.rent80.com"` → `"app.rent80.com"`).
  - `parse_content_js(js: str) -> AdCopy | None`.
  - `parse_overlay_param(content_js_url: str) -> AdCopy | None`. It base64url-decodes and gunzips the `overlay=` query value (strip the leading `=`), then reads the length-prefixed strings that follow the field names `headline`, `description` and `visurl`. Set `method="content_js"` and `confidence=1.0`.
  - `parse_text_ad_ocr(r: OcrResult) -> AdCopy | None`.
  - `parse_image_ad_ocr(r: OcrResult) -> AdCopy`.
  - `needs_vision(r: OcrResult, copy: AdCopy | None) -> bool`.
  - `VISION_SCHEMA: dict`, with all strings plus `confidence`, required, `additionalProperties: false`.
  - `VISION_PROMPT: str`.
  - `extract_image(path: Path, image_id: str, fmt: str, *, cache: ExtractionCache, codex: CodexRunner | None, force_vision: bool, ocr=recognize, warn=log) -> AdCopy`.
- `content.js` rules:
  - Decode the `\xNN` and `\uNNNN` escapes **only**, with a regex. Never use `unicode_escape` on the whole file, because the file is UTF-8 and that corrupts it.
  - Strip U+2066 and U+2069.
  - The rules are **additive**: apply every rule below and merge, so earlier rules win per field.
  - **Video:** `ytimg.com/vi/<id>/` → `video_id`.
  - **Landing:** the first `adurl=<urlencoded>` → `landing_url` (URL-decoded). If `website` is empty, set it to the landing URL's host.
  - **Template:** `'adData': [{…}]` → `headline`, `body` (→ description), `advertiser` (→ brand), `logo`, and the image keys (`squareImage`, `landscapeImage`, `portraitImage`, whichever are present) → `image_urls`.
  - **Text (when there's no `video_id`):** the markup strings between tags that contain letters and no JS punctuation, skipping `"Single Ad Rendering Service"`. In order: brand, display URL, headline, description, then the rest are sitelinks.
  - **Video/app markup (when there's a `video_id`):** the first string is the brand, the longest string is the description, and the first string in `CTA_WORDS = {"Install", "Download", "Get", "Open", "Learn more", "Visit site", "Shop now", "Sign up", "Book now"}` (case-insensitive) is the CTA. `full_text` is all strings, deduplicated, joined by `\n`.
  - Return None if nothing matched.
  - Set `method = "content_js"`, `confidence = 1.0`, and `website = website_from(display_url)`.
- OCR layout rules:
  - **Text ad:** line 1 is the brand, line 2 is the display URL (must match `^[\w.-]+\.[a-z]{2,}(/\S*)?$` after stripping spaces). The headline is the following lines whose box height is ≥ 1.2 × the median height. The description is everything after.
  - **Image ad:** the brand and display URL come from lines in the top 12% of the image. `cta` is the last short line (≤ 3 words). `full_text` is all lines.
  - **`needs_vision`** is true when there are no lines, the confidence is < 0.6, or `copy is None`.
- `extract_image` flow:
  1. If `force_vision` is not set, check the cache for an OCR entry and run `ocr` if there is none.
  2. If `needs_vision` or `force_vision`, check the cache for a `codex` entry, then call `codex.extract`.
  3. If that returns None (limit reached) or raises `CodexError`, warn and keep the OCR copy.
  4. If `OcrUnavailable` is raised and there's no Codex, raise it.
  5. Cache every engine result under `(image_id, method, model)`.

- [ ] **Step 1: Capture one more fixture.** POST one `GetCreativeById` for `AR04752318142645010433` / `CR01331437259009818625` (the encoding is in Task 5) and save the response as `tests/fixtures/ads_transparency/creative_detail_video.json`. One request only.

- [ ] **Step 1b: Write failing tests.**

```python
def test_parse_content_js_text_fixture():
    c = parse_content_js((FIX / "content_text.js").read_text(encoding="utf-8"))
    assert (c.brand, c.headline) == ("Hotels70", "Apartahotel por Mes - Apartahotel a Buen Precio")
    assert c.display_url == "www.hotels70.com/apartahoteles/estancias" and c.website == "hotels70.com"
    assert c.description.startswith("¿Quieres más espacio?")
    assert "Cancelación Gratuita" in c.sitelinks and len(c.sitelinks) == 9 and c.method == "content_js"
def test_parse_content_js_image_template_fixture():
    c = parse_content_js((FIX / "content_image.js").read_text(encoding="utf-8"))
    assert (c.brand, c.headline) == ("Rent80", "Noleggio Auto a Prezzi Bassi")
    assert c.description == "Prenota in pochi click. Ritiro rapido in aeroporto o centro città."
    assert c.logo_url.endswith("/simgad/11202933705113574445")
    assert any(u.endswith("/simgad/4554907277327000602") for u in c.image_urls)
def test_parse_content_js_video_app_install_fixture():
    c = parse_content_js((FIX / "content_video.js").read_text(encoding="utf-8"))
    assert c.video_id == "KVHq8nrh0GI" and c.brand == "eSIM70" and c.cta == "Install"
    assert c.description == "Get affordable travel data abroad with quick eSIM setup and no physical SIM swap."
    assert c.landing_url.startswith("https://play.google.com/store/apps/details?id=com.esim70.app")
def test_parse_overlay_param():  # OVERLAY_URL = the second variation's content.js URL in creative_detail_video.json (captured in Step 1)
    c = parse_overlay_param(OVERLAY_URL)
    assert c.headline.startswith("eSIM for 190+ Countries") and c.description == "Install app - eSIM70: eSIM For Travel Data" and c.display_url == "play.google.com/store"
def test_parse_content_js_unknown_returns_none(): assert parse_content_js("var x = 1;") is None
def test_parse_text_ad_ocr_synthetic():  # build OcrResult: brand h=.05, url h=.04, two headline lines h=.08, three description lines h=.05
    c = parse_text_ad_ocr(r)
    assert (c.brand, c.display_url, c.website) == ("flights70.com", "www.flights70.com/", "flights70.com")
    assert c.headline == "Book Cheap Flight Tickets - Find Cheapest Flight Tickets" and c.description.startswith("Why waste hours")
def test_parse_text_ad_ocr_rejects_non_search_layout(): ... # second line not a URL → None
def test_needs_vision_on_low_confidence(): ...             # confidence .5 → True; .9 with copy → False
def test_extract_image_uses_cache_before_ocr(tmp_path): ...  # pre-populated cache → fake ocr never called
def test_extract_image_falls_back_to_codex_and_records_model(tmp_path): ...
    # fake ocr returns empty → fake codex returns {...,"confidence":.9} → method=="codex", model=="gpt-5.6-luna"
def test_extract_image_keeps_ocr_when_codex_fails(tmp_path): ... # codex raises CodexError → method=="apple_vision"-like fake method, warn called once
def test_extract_image_real_ocr_text_fixture(tmp_path): ...      # skip if OcrUnavailable; headline starts "Book Cheap Flight Tickets"
```

- [ ] **Step 2: Run** `uv run pytest tests/tools/ads_transparency/test_copy.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** following the rules above.
- [ ] **Step 4: Run.** Expected: PASS, including the real-OCR fixture test on this Mac.
- [ ] **Step 5: Commit** `feat(ads-transparency): ad copy extraction from content.js, OCR and Codex`.

---

### Task 7: Stages, `scrape()` API, CLI wiring and CSV export

**Files:** Create `src/adrenalin/tools/ads_transparency/{stages,api}.py` and fill in `__init__.py` (`TOOL`). Test `tests/tools/ads_transparency/test_api.py`, `tests/tools/ads_transparency/test_cli.py`.

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `ScrapeOptions(advertiser_id: str, region: str = "anywhere", start: date | None = None, end: date | None = None, variations: bool = False, vision: bool = False, vision_model: str = DEFAULT_VISION_MODEL, vision_limit: int = 200, wait_on_block: bool = False, stage: Literal["list", "fetch", "extract", "export"] | None = None, out: Path = Path("out"), proxies: tuple[str, ...] = ())`.
  - `ScrapeResult(csv_path: Path, rows: int, blocked_until: float | None, failed: dict[str, int], vision_skipped: int)`.
  - `scrape(options, *, session: PacedSession | None = None, ocr=recognize, codex: CodexRunner | None = None, log=…) -> ScrapeResult`.
  - `list_creatives(advertiser_id, region="anywhere", start=None, end=None, *, session=None) -> Iterator[ListedCreative]`, a thin wrapper over `iter_pages` for other tools.
  - `CSV_FIELDS: tuple[str, ...]`, exactly the spec's column order.
  - `TOOL = Tool("ads-transparency", …)`.
- Stage design (`stages.py`), with queue stage names `list`, `detail`, `fetch` and `extract`:
  - **list:** an item keyed `page:<cursor or "">`. It calls `client.search`. For each creative it puts `detail:<cr>` (if variations) or `fetch:<cr>#1` with payload `{creative fields…, "variation": 1, "preview": {...}}`. Then it puts the next `page:<cursor>`. The meta key `seen_cursors` holds a JSON list for loop detection across resumes.
  - **detail:** `client.variations` → `fetch:<cr>#<n>` for n = 1…len.
  - **fetch:** for an image preview, download to `<entity>/images/<image_id>.png` unless it exists (writing to a `.part` file then renaming). For `content_js`, download the text into the result. Then put `extract:<cr>#<n>`.
  - **extract:** for `content_js`, `parse_content_js` (None → warning "content.js not parsed for CR…", blank copy). For video, also fetch `https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<id>&format=json` → `video_title`. Catch `Blocked`, `requests.RequestException` and non-200 responses → blank title, warn once; oEmbed must never stop the stage. If `parse_content_js` returns None, try `parse_overlay_param(preview.url)`. For an image, `extract_image(...)`; if it returns the OCR copy because the Codex limit was hit, add `"vision_pending": true`. The result is the copy fields dict.
  - Each stage is a `Stage` with the throttles from Global Constraints. Concurrency: list 1, detail 1, fetch 4, extract 2 (plus Codex's own cap).
- `scrape()` flow:
  1. Resolve the name from `out/.cache/advertisers.json`, else `client.advertiser_name`. None means `ValueError("No creatives found for AR…")`.
  2. Call `entity_dir`. If no `session` is passed, build `PacedSession(Cooldown(cache_dir(out)/"cooldown.sqlite3"), {"adstransparency.google.com": (6, 2)}, proxies=options.proxies, wait_on_block=options.wait_on_block)`. Build `ExtractionCache(cache_dir(out)/"extract.sqlite3")`. Build `CodexRunner(options.vision_model, options.vision_limit)` if not passed.
  3. The run dir is `runs/<region>_<start or all>_<end or all>` plus `_variations` when set. Open `WorkQueue(run/state.sqlite3)`. Meta `options` stores `{"vision": bool}`, and meta `list_done` stores the ISO date the list stage last finished (set by the list worker when a page has no cursor).
     - Empty queue: seed `page:`.
     - `list_done` set and window open (`end is None or end >= list_done`): clear `seen_cursors`, `requeue("list", ["page:"])`. List workers `put(..., update_payload=True)` for fetch items, so listing fields refresh without re-extracting.
     - `options.vision` and stored vision False: requeue extract items whose result method is not `codex` or `content_js`. Store the new options.
     - Always requeue extract items whose result has `"vision_pending": true` (set when `CodexRunner` returned None).
     - List worker: if `client.search` raises `ValueError` for a non-empty cursor, log "cursor expired, restarting listing", clear `seen_cursors`, put `page:` again (requeue it if it's done) and complete the current item.
  4. Run the `Pipeline` with the stages selected by `options.stage`. `export` runs no stages.
  5. Always export: `write_csv_atomic(run/ads.csv, CSV_FIELDS, rows, sorted by (creative_id, variation))`. Each row is the **current fetch-item payload** (listing fields) merged with the extract result (copy fields), so re-listing refreshes `last_shown` and `days_shown`.
  6. Log a summary.
  7. Close the queue and session in `finally`.
- CLI:
  - `--days` conflicts with `--start` and `--end`, and `today` is `date.today()`.
  - Exit codes: 0 when done, 2 when blocked (message `blocked until HH:MM; rerun the same command to continue`), 1 when any item failed (message lists the counts per stage, `rerun to retry`) or on another error.
  - Print the CSV path and the row count to stdout.

- [ ] **Step 1: Write failing tests.** Build a fake transport (Task 2's `transport=` hook) that routes by URL:
  - `SearchCreatives` returns `search_page.json` with `"2"` removed.
  - `GetCreativeById` returns `creative_detail.json`.
  - `simgad` returns `simgad_text.png` bytes.
  - `content.js` returns `content_video.js` for video creatives, otherwise `content_text.js`.
  - `oembed` returns `oembed.json`.

  Use a fake OCR that returns a synthetic search-ad `OcrResult`, and `PacedSession(intervals={}, sleep=lambda s: None)`.

```python
def test_scrape_writes_one_row_per_creative(tmp_path):
    res = scrape(ScrapeOptions("AR04752318142645010433", out=tmp_path), session=fake, ocr=fake_ocr)
    rows = list(csv.DictReader(res.csv_path.open()))
    assert res.csv_path == tmp_path / "ads-transparency/lascade-llc/runs/anywhere_all_all/ads.csv"
    assert len(rows) == 40 == res.rows and tuple(rows[0]) == CSV_FIELDS
    assert {r["variation"] for r in rows} == {"1"}
    assert all(r["transparency_url"].endswith(f"/creative/{r['creative_id']}?region=anywhere") for r in rows)
    v = next(r for r in rows if r["format"] == "video"); assert v["video_title"] == "Rotating Tiles video" and v["extraction_method"] == "content_js"
    i = next(r for r in rows if r["image_file"]); assert (tmp_path / "ads-transparency/lascade-llc" / i["image_file"]).exists()
def test_scrape_variations_mode_one_row_per_variation(tmp_path): ...  # detail fixture dedupes to 2 → 80 rows; variation in {"1","2"}; run dir ends with _variations
def test_rerun_with_closed_window_fetches_nothing_new(tmp_path): ...  # end in the past → second scrape makes no RPC or simgad calls, same 40 rows
def test_rerun_with_open_window_relists_and_refreshes(tmp_path): ...  # second scrape with fixture days_shown +1 → SearchCreatives called again, CSV days_shown updated, no new simgad calls
def test_rerun_with_vision_requeues_ocr_rows(tmp_path): ...           # first run OCR only; rerun vision=True with fake codex → image rows method=="codex"
def test_codex_limit_rows_requeued_next_run(tmp_path): ...            # vision_limit=1 → one codex row, others vision_pending; rerun → more codex rows
def test_expired_cursor_restarts_listing(tmp_path): ...               # page-2 request returns HTML 400 once → listing restarts, final 40 rows, no duplicates
def test_oembed_failure_blanks_title_without_failing(tmp_path): ...   # oembed 401 → video_title=="" and res.failed=={}
def test_failed_items_exit_1_and_retry_next_run(tmp_path): ...        # one simgad 500 → res.failed=={"fetch":1}; rerun with healthy transport → 40 rows, failed=={}
def test_block_mid_list_exports_partial_and_reports(tmp_path): ...
    # page 1 has cursor; page 2 request → sorry redirect → res.blocked_until set; CSV has page-1 rows; rerun after fake clock passes → completes
def test_zero_creatives_in_window_writes_header_only(tmp_path): ...    # filtered search → {"1": []}, unfiltered → fixture; rows == 0; header present
def test_stage_extract_only_does_not_call_rpc(tmp_path): ...           # after a blocked run, scrape(stage="extract") → no SearchCreatives calls
def test_existing_folder_for_other_advertiser_raises(tmp_path): ...    # pre-create entity.json {"id": "AR999", "name": "Lascade LLC"} → EntityCollision

# test_cli.py
def test_cli_parses_days_and_region(monkeypatch): ...  # monkeypatch api.scrape capturing options; main(["ads-transparency","AR1","--region","IN","--days","7"]) → start == today-6, end == today, region "IN"
def test_cli_rejects_days_with_start(): ...            # SystemExit 2
def test_cli_blocked_exits_2(monkeypatch, capsys): ... # scrape returns blocked_until → exit 2, "rerun" in stderr
```

- [ ] **Step 2: Run** `uv run pytest tests/tools/ads_transparency -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `stages.py`, `api.py` and `__init__.py` as specified.
- [ ] **Step 4: Run** `uv run pytest -v`. Expected: the whole suite passes, and no files are left outside `tmp_path`.
- [ ] **Step 5: Commit** `feat(ads-transparency): staged scrape pipeline, API and CLI`.

---

### Task 8: Live verification and docs

**Files:** Create `tests/tools/ads_transparency/test_live.py`. Modify `CLAUDE.md` and `README.md`.

- [ ] **Step 1:** Add `@pytest.mark.live def test_live_lascade_last_7_days(tmp_path)`. It runs `scrape(ScrapeOptions("AR04752318142645010433", region="IN", start=today-6, end=today, out=tmp_path))` and asserts `rows > 0`, at least one row with a non-empty `headline`, and every `extraction_method` in `{"content_js", "apple_vision", "tesseract", "codex"}`, or empty only for `content.js` parse misses.
- [ ] **Step 2: Run** `uv run pytest -m live -v -s`. Expected: PASS, with the run time and row count printed. If it ends blocked, report that rather than retrying in a loop.
- [ ] **Step 3: Run the CLI by hand:** `uv run adrenalin ads-transparency AR04752318142645010433 --region IN --days 7`. Open `out/ads-transparency/lascade-llc/runs/IN_<start>_<end>/ads.csv` and spot-check 5 rows against their `transparency_url`s. Record the duration and the folder size.
- [ ] **Step 4: Update `CLAUDE.md`.**
  - Replace "There is no `[project.scripts]` entry, test suite…" with the real commands: `uv run adrenalin <tool> …`, `uv run pytest`, `uv run pytest tests/path::test_name`, `uv run pytest -m live`.
  - Describe the shared modules (`common/http`, `pipeline`, `ocr`, `cache`, `output`, `geo`) and the rule that a new tool exposes `TOOL` plus an `api.py`.
  - Point to `CONTEXT.md` and `docs/adr/`.
  - Remove the "intended design" wording.
- [ ] **Step 5: Update `README.md`** with a one-paragraph description, install instructions (`uv sync`, `brew install tesseract` for non-Mac or a fallback), and an `ads-transparency` usage example.
- [ ] **Step 6:** Delete `out/` from the live run if the user doesn't want to keep it (ask). Clean the scratchpad `probe/` folder.
- [ ] **Step 7: Commit** `docs: document adrenalin commands and ads-transparency tool`.
