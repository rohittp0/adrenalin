# Sampled runs overlap listing and reading through a random head start

When an Advertiser's Inventory is too large to read in full, the run reads only a Copy sample, and the sample has to cover every launch week. Those quotas can only be filled once the whole Inventory is known. We still overlap listing and reading as in ADR 0001: while pages arrive, each listed Creative is sent to reading at random with a probability sized from Google's count estimate. That gives a uniform random head start, and the reading backlog keeps throttling the listing. When the listing finishes, the run tops up any launch-week buckets below their minimum, then keeps sampling where new Messages are still turning up until Coverage reaches the target. Random admission is safe because Google's listing order is unrelated to launch date: we checked this on Lascade's complete listing, where every quarter of the listing spans the same date range.

## Considered Options

- **List first, then sample:** simpler, with exact quotas from the start. Rejected because listing would no longer be throttled by reading, so its requests to Google would come in one stretch (about 900 pages for 90K Creatives), which ADR 0001 exists to avoid. Downstream stages would also sit idle for the whole listing.

## Consequences

- Runs whose Inventory is within the full-read limit admit every Creative, so they behave exactly as before.
- If Google ever lists Creatives in launch-date order, the head start stops being random. The quota top-up still guarantees every bucket its minimum, but the sample would be skewed until it does.
