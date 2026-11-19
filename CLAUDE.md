# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Adrenalin is a set of competitor-analysis tools for marketing teams: one CLI, `adrenalin <tool_name> [args...]`, that dispatches to sub-tools. The only tool so far is `ads-transparency` (Google Ads Transparency creatives and ad copy to CSV). Domain terms (Advertiser, Creative, Variation, Preview, Shown period) are defined in `CONTEXT.md`; use them. Decisions that should not be undone ("don't simplify this into a sequential loop", "plain HTTP, not a browser") are in `docs/adr/`.

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
   - `http.py`: `PacedSession` (per-host pacing, proxies), `Cooldown` (block state shared across runs in `out/.cache/cooldown.sqlite3`), `Blocked`, `HostChecker` (does a host exist: one `GET https://<host>/`).
   - `pipeline.py`: `Pipeline` and the SQLite-backed `WorkQueue` for staged, resumable, backpressured runs (see ADR 0001).
   - `ocr/`: `recognize()` (Apple Vision on macOS, else Tesseract) and, separately, `CodexRunner` (the `codex` CLI as a vision model, with a per-run call cap and a breaker when the binary is missing). Which of them reads an image is decided by the tool (`ads_transparency/copy.py:extract_image`).
   - `cache.py`: `ExtractionCache` of extraction results, shared by all runs; keys are namespaced (`simgad:<id>`, `domain:<host>`).
   - `output.py`: `entity_dir`, `cache_dir`, `write_csv_atomic` (also defuses spreadsheet formulas), slugs.
   - `geo.py`: region (ISO code or `anywhere`) to Google geo ID.
   - `tool.py`: the `Tool` dataclass.

**Refactor shared logic into `common/` aggressively.** When a second tool needs logic that already exists inside another tool, move it into `common/` instead of duplicating it. Check `common/` before writing a new helper inside a tool.

`ads-transparency` (`tools/ads_transparency/`): `rpc.py` (Google's internal RPCs over plain HTTP), `stages.py` (the queue stages List, Detail, Fetch and Extract), `copy.py` (ad copy: `content.js` Previews, OCR layout rules for `simgad` images, the Codex fallback and the OCR domain check), `api.py` (`scrape()`, `ScrapeOptions`, and Export: CSV columns and rows).

Output goes to `out/<tool>/<entity>/runs/<run>/` (git-ignored).
