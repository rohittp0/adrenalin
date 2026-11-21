# Staged pipeline with backpressure, not fixed request pacing

Scraping tools run as independent stages (List → Fetch → Extract → Export) that share a SQLite-backed queue and run concurrently in one process. Each upstream stage pauses while the downstream backlog is above a small buffer, so the slowest stage (usually OCR or Codex) sets the request rate to Google, and a fixed request spacing is only a minimum. We chose this so that a run that will spend hours extracting anyway also spreads its blockable requests over those hours instead of bursting them. A cooldown or `/sorry/` block on the List stage also no longer stops downloading and extraction, and every stage can resume from the checkpoint.

## Consequences

- Sampled runs keep this overlap: see ADR 0003.

- Do not "simplify" this into a sequential loop with a sleep: that reintroduces bursts and makes OCR wait on cooldowns.
- The cooldown state is shared across runs (`out/.cache/cooldown.sqlite3`) because Google blocks the IP, not the run.
- A blocked stage also stops every stage that throttles on it. Otherwise the upstream stage is no longer held back by a backlog that cannot drain, and races ahead into a block of its own. The first live run (Rio, 2026-11-19) did exactly that: the List stage was blocked about 85 s after the content.js host. Stages that don't wait on the blocked one keep going.
- Downloads from hosts that are blocked separately get separate stages (Fetch for `simgad` images, Script for `content.js`), so a block on one host doesn't stop the other's downloads.
