# Sampled runs overlap listing and reading through a random head start

When an Advertiser's Inventory is too large to read in full, the run reads only a Copy sample, and the sample has to cover every launch week. Those quotas can only be filled once the whole Inventory is known. We still overlap listing and reading as in ADR 0001: while pages arrive, each listed Creative is sent to reading at random with a probability sized from Google's count estimate. That gives a uniform random head start, and at a large `p` the reading backlog still throttles the listing (at a small `p` it can't: see Consequences). When the listing finishes, the run tops up any launch-week buckets below their minimum, then keeps sampling where new Messages are still turning up until Coverage reaches the target. Sampling rounds overlap reading too: the next round is chosen while the last novelty round is still being read, so novelty is judged one round late, and a round's lack of new Messages is judged only on the Creatives whose copy reading has finished. Random admission is safe because Google's listing order is unrelated to launch date: we checked this on Lascade's complete listing, where every quarter of the listing spans the same date range.

## Considered Options

- **List first, then sample:** simpler, with exact quotas from the start. Rejected because listing would no longer be throttled by reading, so its requests to Google would come in one stretch (about 900 pages for 90K Creatives), which ADR 0001 exists to avoid. Downstream stages would also sit idle for the whole listing.

## Consequences

- Runs whose Inventory is within the full-read limit admit every Creative, so they behave as before, unless `--max-ads` is below the estimate: then even a small Inventory is sampled.
- `p` is stored the first time a page is listed and never recomputed, so a rerun of the same run folder with another `--max-ads` or `--coverage` cannot change the head start; they only change how far the rounds after it go (the run logs a note when `--max-ads` would have given another `p`). Use a new output folder to start over.
- At a small `p` (for example 2,000 of 90K Creatives) reading never builds the backlog needed to throttle listing, so the listing is paced only by the RPC interval (about one page every 6 s). The throttle matters only when `p` is large.
- If Google ever lists Creatives in launch-date order, the head start stops being random. The quota top-up still guarantees every bucket its minimum, but the sample would be skewed until it does.

## Revision: representative stratified sampling is the default

Novelty-targeted selection and a pooled singleton score do not justify a
population Coverage claim. Following the user's approved change, default runs
now collect the complete Inventory, then select a stratified random Copy sample.
This intentionally replaces the random head start in representative mode:
unknown stratum sizes cannot give the final proportional allocation yet.
The staged queue, concurrent reading, backpressure and resumability remain.
Legacy head-start/novelty behavior is available explicitly with `--sampling novelty`.

Each observable Ads account × launch period × running/stopped stratum receives
up to three slots. Remaining slots are allocated proportionally by Inventory,
with capacity limits and deterministic largest-remainder tie-breaking. The
default target is 2,000 (overridden by `--max-ads`); minimums can exceed it.
Selection ranks all group members by a saved random-seeded hash. Existing
reads never substitute for independently selected members. The seed and design
are persisted before queue admission, allowing interruption recovery.

Migrating old runs retains all copy, marks nonselected reads exploratory, and
tops up missing selected members. Representative design weights are N_h/n_h,
not N_h divided by successful reads. Missing copy is not imputed. Re-running an
unchanged complete Inventory with the same target preserves the cohort; changing
the Inventory or target recomputes the design using the saved seed. Unknown
accounts remain unknown rather than inferred from post-selection outcomes.
No population Coverage score or 95% stopping claim is emitted for this mode.
