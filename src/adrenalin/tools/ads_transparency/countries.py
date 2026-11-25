"""Shown countries of the weighted Copy sample: which Creatives get them, the `countries` stage's queue items,
and `countries.csv`."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date

from adrenalin.common.pipeline import Item, WorkQueue
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.rpc import Query, parse_shown_countries
from adrenalin.tools.ads_transparency.sample_queue import SampleTally, listing_stage, sample_groups

COUNTRIES = "countries"


def _rank(creative_id: str) -> str:
    return hashlib.sha1(creative_id.encode()).hexdigest()


def country_targets(tally: SampleTally, admitted: Iterable[str], cap: int) -> list[str]:
    """The Creatives whose Shown countries are fetched: the representative sample in stratified mode; otherwise
    (novelty, `--all`) the `cap` admitted Creatives with the lowest SHA-1, a random subset that stays stable as more
    Creatives are admitted."""
    if tally.representative is not None:
        return sorted(tally.representative)
    return sorted(set(admitted), key=_rank)[:cap]


def queue_countries(q: WorkQueue, variations: bool, query: Query, today: date, max_ads: int | None) -> int:
    """Queue a `countries` item for every target Creative not queued yet; returns how many were new."""
    tally = sample_groups(q, variations, query, today)
    admitted = {it.payload["creative_id"] for it in q.items(listing_stage(variations))}
    targets = country_targets(tally, admitted, max_ads or sampling.DEFAULT_SAMPLE_SIZE)
    return sum(q.put(COUNTRIES, cr, {"creative_id": cr}) for cr in targets)


COUNTRY_FIELDS: tuple[str, ...] = (
    "creative_id", "status", "country", "first_shown", "last_shown", "impressions_min", "impressions_max",
    "sample_weight",
)


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _blank(v: int | None) -> int | str:
    return "" if v is None else v


def country_weights(items: list[Item], tally: SampleTally, sampled: bool) -> dict[str, float | str]:
    """Each Creative's weight in `countries.csv`. Stratified: its Copy-sample weight. Otherwise (novelty, `--all`)
    the countries are a subset of the processed Creatives, so their copy weights are scaled up to the total copy
    weight of all processed Creatives. "" for a Creative without a copy weight."""
    copy = {it.key: tally.weight(it.key, sampled) for it in items}
    if tally.representative is not None:
        return copy
    total = sum(w for cr in tally.processed if isinstance(w := tally.weight(cr, sampled), float))
    fetched = sum(w for it in items if it.status == "done" and isinstance(w := copy[it.key], float))
    scale = total / fetched if fetched else 1.0
    return {cr: w * scale if isinstance(w, float) else "" for cr, w in copy.items()}


def country_rows(q: WorkQueue, tally: SampleTally, sampled: bool) -> list[dict]:
    """`countries.csv` rows: one per Shown country (`shown`), or one with a blank country for a Creative fetched
    with none reported (`none_reported`) or not fetched yet (`pending`, failures included)."""
    items = q.items(COUNTRIES)
    weights = country_weights(items, tally, sampled)
    rows = []
    for it in items:
        base = {"creative_id": it.key, "sample_weight": weights[it.key]}
        blank = {"country": "", "first_shown": "", "last_shown": "", "impressions_min": "", "impressions_max": ""}
        if it.status != "done":
            rows.append({**base, "status": "pending", **blank})
            continue
        shown = parse_shown_countries((it.result or {}).get("shown_countries"))
        if not shown:
            rows.append({**base, "status": "none_reported", **blank})
        for c in shown:
            rows.append({**base, "status": "shown", "country": c.country, "first_shown": _iso(c.first_shown),
                         "last_shown": _iso(c.last_shown), "impressions_min": _blank(c.impressions_min),
                         "impressions_max": _blank(c.impressions_max)})
    rows.sort(key=lambda r: (r["creative_id"], r["country"]))
    return rows


def countries_pending(q: WorkQueue) -> int:
    return sum(1 for it in q.items(COUNTRIES) if it.status != "done")


def countries_message(n: int) -> str:
    return f"{n:,} Creatives still need Shown countries; rerun the same command to continue"
