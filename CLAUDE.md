# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Adrenalin is a set of competitor-analysis tools for marketing teams: one CLI, `adrenalin <tool_name> [args...]`, that dispatches to sub-tools. There are two tools: `ads-transparency` (Google Ads Transparency creatives and ad copy to CSV) and `meta-ad-library` (one Facebook Page's Meta Ad Library Creatives to CSV). Domain terms (Advertiser, Creative, Variation, Preview, Shown period) are defined in `CONTEXT.md`; use them. Decisions that should not be undone ("don't simplify this into a sequential loop", "plain HTTP, not a browser") are in `docs/adr/`.

## Commands

Python 3.13, managed with `uv`.

```bash
uv sync                                          # install deps
uv run adrenalin <tool> [args...]                # run a tool, e.g. uv run adrenalin ads-transparency AR0475... --region IN --days 7
uv run pytest                                    # offline tests (live tests are deselected by default)
uv run pytest tests/tools/ads_transparency/test_copy.py::test_name   # one test
uv run pytest -m live -v -s                      # live tests: real requests to Google, slow, may be blocked
uv add <pkg> / uv add --dev <pkg>                # add a runtime / dev dependency
```

No linter is configured. OCR needs `tesseract` on non-macOS (macOS uses Apple Vision via `ocrmac`, with Tesseract as fallback).

The existing virtualenv is at `venv/`, not uv's default `.venv/`. `uv run` and `uv sync` use `.venv/` unless `UV_PROJECT_ENVIRONMENT=venv` is set.

## Architecture

Code lives in `src/adrenalin/` in three layers:

1. **`cli.py`** discovers tools by scanning `adrenalin.tools.*` packages for a module-level `TOOL` (a `common.tool.Tool`: name, help, `add_arguments`, `run`). It holds no tool-specific logic and has no hard-coded tool list.
2. **`tools/<name>/`** are self-contained tools. A new tool must expose `TOOL` in its package `__init__.py` and a callable Python API in `api.py` (the CLI is a thin wrapper over it), so other tools can call it programmatically. Tools never shell out to `adrenalin`.
3. **`common/`** is shared code:
   - `http.py`: `PacedSession` (per-host pacing, proxies, per-host `HostPolicy`), `Cooldown` (block state shared across runs in `out/.cache/cooldown.sqlite3`), `Blocked`, `HostChecker` (does a host exist: one `GET https://<host>/`), `get_ok` (a GET that must answer 200). `PacedSession.block` puts a host in cooldown for a block a tool infers from several responses.
   - `pipeline.py`: `Pipeline` and the SQLite-backed `WorkQueue` for staged, resumable, backpressured runs (see ADR 0001). `fatal` exceptions stop the run and come back in `RunResult.fatal`; the tool reports them.
   - `progress.py`: `QueueProgress`, tqdm bars refreshed from a `WorkQueue`'s counts.
   - `ocr/`: `recognize()` (Apple Vision on macOS, else Tesseract) and, separately, `CodexRunner` (the `codex` CLI as a vision model, with a per-run call cap and a breaker when the binary is missing). Which of them reads an image is decided by the tool (`ads_transparency/copy.py:extract_image`).
   - `cache.py`: `ExtractionCache` of extraction results, shared by all runs; keys are namespaced (`simgad:<id>`, `domain:<host>`).
   - `output.py`: `entity_dir`, `cache_dir`, `write_csv_atomic` (also defuses spreadsheet formulas), `write_bytes_atomic`, `write_json_atomic`, slugs.
   - `geo.py`: region (ISO code or `anywhere`) to Google geo ID, and a country geo ID back to its ISO code.
   - `runlog.py`: `console_logging(verbose)` (the `adrenalin` logger to stderr above tqdm bars) and `run_log(run_dir)` (`run.log`; a library caller's root handlers still get only what they got before). Modules log through `logging.getLogger(__name__)`.
   - `cli.py`: shared run flags (`add_run_arguments`), Shown period flags and validation (`add_period_arguments`, `resolve_period`), `finish` (outcome lines and exit code), `run_cli` (exception to exit code). Exit codes: 0 ok, 1 failed items or errors, 2 blocked, 3 incomplete or pending.
   - `listing.py`: `PaginationLoop` and cursor-paginated listings on a `WorkQueue` (one queue item per page, loop detection across reruns), `restart_listing` (a relist from page 1), and `FirstClaim` / `restart_expired_cursor` (a cursor carried over from an earlier run that has expired).
   - `tool.py`: the `Tool` dataclass.

**Refactor shared logic into `common/` aggressively.** When a second tool needs logic that already exists inside another tool, move it into `common/` instead of duplicating it. Check `common/` before writing a new helper inside a tool.

`ads-transparency` (`tools/ads_transparency/`): `rpc.py` (Google's internal RPCs over plain HTTP), `stages.py` (the queue stages List, Sample, Detail, Countries, Fetch (images), Script (`content.js`) and Extract; see ADR 0003 for Sample), `countries.py` (Shown countries: which Copy-sample Creatives get them, and `countries.csv`), `sampling.py` (pure Copy sample rules: Messages, Coverage, buckets, allocation, weights), `sample_queue.py` (the Copy sample on the queue: `held` Creatives, groups and per-Creative weights, and the Sample stage's rounds), `copy.py` (ad copy and Copy status: `content.js` Previews, OCR layout rules for `simgad` images, the Codex fallback and the OCR domain check), `api.py` (`scrape()`, `ScrapeOptions`, and Export: CSV columns and rows).

`meta-ad-library` (`tools/meta_ad_library/`): `parse.py` (pure parsing of Meta's GraphQL answers into Creatives, Cards and Variations), `client.py` (the GraphQL client; the hard-coded `doc_id`s and the block and "document not found" detection), `hosts.py` (Meta host rules: pacing and which answers are blocks), `stages.py` (the queue stages List, Detail (EU reach, `--eu-reach`), Fetch (media) and OCR (`--ocr`)), `api.py` (`scrape()`, `MetaOptions`, and Export: CSV columns and rows). See ADR 0004.

Output goes to `out/<tool>/<entity>/runs/<run>/` (git-ignored).
