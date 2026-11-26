# Staged pipeline with backpressure, not fixed request pacing

Scraping tools run as independent stages (List → Fetch → Extract → Export) that share a SQLite-backed queue and run concurrently in one process. Each upstream stage pauses while the downstream backlog is above a small buffer, so the slowest stage (usually OCR or Codex) sets the request rate to Google, and a fixed request spacing is only a minimum. We chose this so that a run that will spend hours extracting anyway also spreads its blockable requests over those hours instead of bursting them. A cooldown or `/sorry/` block on the List stage also no longer stops downloading and extraction, and every stage can resume from the checkpoint.

## Consequences

- Sampled runs keep this overlap: see ADR 0003.
- Do not "simplify" this into a sequential loop with a sleep: that reintroduces bursts and makes OCR wait on cooldowns.
- The cooldown state is shared across runs (`out/.cache/cooldown.sqlite3`) because Google blocks the IP, not the run.
- A blocked stage also stops every stage that throttles on it. Otherwise the upstream stage is no longer held back by a backlog that cannot drain, and races ahead into a block of its own. The first live run (Rio, 2026-11-19) did exactly that: the List stage was blocked about 85 s after the content.js host. Stages that don't wait on the blocked one keep going.
- Downloads from hosts that are blocked separately get separate stages (Fetch for `simgad` images, Script for `content.js`), so a block on one host doesn't stop the other's downloads. This holds for downloads done again too: a content.js read by an older parser whose script was deleted goes back through Script, never downloaded inside Extract, which every stage throttles on.

## RPC throughput

Listing and country/detail lookups share an adaptive host pace, replacing the
original fixed 6-second base. It starts at 3 seconds, reduces the interval by 10%
after 20 clean responses, and stops accelerating at a user-selected 1.2-second
base. A CAPTCHA successfully solved during
the browser handoff preserves the current pace: only the replayed response reaches
the pacing controller. An unresolved CAPTCHA/block or a 429 (including one after
solving) doubles the interval immediately; three consecutive network/5xx failures also
double it, up to 30 seconds. Existing Retry-After, cooldown, CAPTCHA handling and
pipeline backpressure remain in force. Adaptive jitter is ±30% of the base,
clamped so request starts remain at least 1.2 seconds apart. The shared pacing
controller likewise respects each adaptive host's configured floor after jitter.
Pace changes are logged; each new session starts at 3 seconds again, while
persisted host cooldown deadlines survive restarts. Live throughput and Google's
tolerance of the lower interval have not yet been measured.

`--phase SECONDS` overrides the RPC policy with constant request-start spacing:
any positive finite number of seconds, with no jitter or adaptive adjustment.
It overrides the adaptive floor too. Cooldowns, Retry-After and CAPTCHA handling
still apply, and latency or downstream backpressure can make spacing longer.
The override is per session and does not change image or Script pacing. The Python
API exposes it as `ScrapeOptions.phase` and `make_session(phase=...)`; callers
supplying their own session control that session's pacing themselves.

## Script throughput

The Script stage has three workers to overlap slow responses while request starts
share one adaptive host pace. It starts at a 2.5-second base, accelerates by 10%
after 30 clean responses to a 1.5-second floor, and doubles immediately on
HTTP 429/CAPTCHA or after three consecutive network failures/5xx responses, up to
a 15-second ceiling. Failure streaks follow response completion order across
workers and reset on a clean response or a triggered slowdown. An isolated
disconnect or timeout backs off only its own retry, without slowing the host. The earlier 0.5-second block experience rules out using that
aggressive floor. Existing downstream backpressure and block propagation remain.
Transient script-download connection failures and timeouts get at most two
additional GET attempts through the same pacing/cooldown checks. HTTP block
responses are not treated as transient network errors.

Pacing uses a separate per-host gate for request starts. Its sleeping worker must
not hold the adaptive-state lock: doing so delays completed requests while other
workers wait for their next start. Response feedback takes only the short state
lock, and changes to the pacing base are logged to distinguish backoff from slow
network transfers.

HTTP 429 retry backoff is a persisted per-host deadline (`rate:<host>` in the
cooldown database), not a worker-local sleep. Other workers, derived sessions
and subsequent runs honor it before sending requests. This soft retry delay is
waited automatically; exhausted retries still use the ordinary hard-block policy.
Already in-flight requests cannot be withdrawn, but queued requests recheck the
deadline after pacing. Other hosts continue independently.
