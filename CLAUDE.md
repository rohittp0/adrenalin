# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Adrenalin is a set of competitor-analysis tools for marketing teams. It is one CLI that dispatches to sub-tools:

```
adrenalin <tool_name> [args...]
```

The repo is currently a bare `uv` scaffold (`main.py` prints a greeting, no dependencies, no tests). The architecture below is the intended design. Follow it when adding the first tools.

## Architecture

There are three layers:

1. **Wrapper / CLI entry point.** It parses `adrenalin <tool_name>`, finds the matching tool and passes the remaining args to it. It holds no tool-specific logic, so adding a tool should never mean editing a hard-coded list in several places.
2. **Tools.** Each tool is a self-contained unit with its own args and output. A tool can call other existing tools programmatically, so every tool needs a callable Python API, not just a CLI path. Tools should not shell out to `adrenalin`.
3. **Common utilities.** This is shared code such as HTTP and scraping helpers, config, output formatting and caching.

**Refactor into common aggressively.** When a second tool needs logic that already exists inside another tool, move it into the common layer instead of duplicating it. Check the common layer before writing a new helper inside a tool.

## Commands

The project uses `uv` with Python 3.13 (see `.python-version`).

```bash
uv sync                    # install deps
uv run main.py             # run the current entry point
uv add <pkg>               # add a runtime dependency
uv add --dev <pkg>         # add a dev dependency (e.g. pytest, ruff)
```

There is no `[project.scripts]` entry, test suite or linter configured yet. When you add them, update this section with the real commands, including how to run a single test.

## Notes

- The existing virtualenv is at `venv/`, not uv's default `.venv/`. `uv run` and `uv sync` will create and use `.venv/` unless `UV_PROJECT_ENVIRONMENT=venv` is set.
