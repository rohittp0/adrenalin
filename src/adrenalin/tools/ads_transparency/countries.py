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


def current_targets(q: WorkQueue, tally: SampleTally, variations: bool, max_ads: int | None) -> list[str]:
    """`country_targets` for the run as it stands: the Creatives `countries.csv` covers."""
    admitted = {it.payload["creative_id"] for it in q.items(listing_stage(variations))}
    return country_targets(tally, admitted, max_ads or sampling.DEFAULT_SAMPLE_SIZE)


def queue_countries(q: WorkQueue, variations: bool, query: Query, today: date, max_ads: int | None) -> int:
    """Queue a `countries` item for every target Creative not queued yet; returns how many were new. Outside
    stratified mode the targets wait for the listing to finish: the lowest hashes of a partial listing are not the
    lowest of the full one, and fetching both would pass the `--max-ads` cap."""
    tally = sample_groups(q, variations, query, today)
    if tally.representative is None and not q.get_meta("list_done"):
        return 0
    return sum(q.put(COUNTRIES, cr, {"creative_id": cr}) for cr in current_targets(q, tally, variations, max_ads))


COUNTRY_FIELDS: tuple[str, ...] = (
    "creative_id", "status", "country", "first_shown", "last_shown", "impressions_min", "impressions_max",
    "sample_weight",
)


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _blank(v: int | None) -> int | str:
    return "" if v is None else v


def country_weights(items: list[Item], tally: SampleTally, sampled: bool) -> dict[str, float | str]:
    """Each Creative's weight in `countries.csv`. Stratified: its Copy-sample (design) weight. Otherwise (novelty,
    `--all`) the fetched Creatives are post-stratified on the Copy sample's groups: group Inventory / Creatives
    fetched in the group. Copy weights would over-correct here, since novelty rounds skew copy reads but not the
    hash-chosen countries subset. "" for a Creative not fetched yet (novelty) or without a design weight."""
    if tally.representative is not None:
        return {it.key: tally.weight(it.key, sampled) for it in items}
    fetched: dict[str, int] = {}
    for it in items:
        if it.status == "done" and (g := tally.group_of.get(it.key)) is not None:
            fetched[g] = fetched.get(g, 0) + 1
    out: dict[str, float | str] = {}
    for it in items:
        g = tally.group_of.get(it.key)
        out[it.key] = tally.groups[g].inventory / fetched[g] if it.status == "done" and g in fetched else ""
    return out


def _target_items(q: WorkQueue, targets: Iterable[str]) -> list[Item]:
    """The `countries` items of `targets`; a target not queued yet gets a stand-in pending item. Items queued by an
    earlier run with other options (another `--max-ads`, mode or cohort) are left out."""
    queued = {it.key: it for it in q.items(COUNTRIES)}
    return [queued.get(cr) or Item(COUNTRIES, cr, {"creative_id": cr}, None, "pending", None) for cr in targets]


def country_rows(q: WorkQueue, tally: SampleTally, sampled: bool, targets: Iterable[str]) -> list[dict]:
    """`countries.csv` rows for `targets`: one per Shown country (`shown`), or one with a blank country for a
    Creative fetched with none reported (`none_reported`) or not fetched yet (`pending`, failures included)."""
    items = _target_items(q, targets)
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


def countries_pending(q: WorkQueue, targets: Iterable[str]) -> int:
    """Targets a rerun can still fetch: not done, and not failed on every attempt (those are reported as failed
    items, and a rerun would not retry them)."""
    exhausted = q.exhausted(COUNTRIES)
    return sum(1 for it in _target_items(q, targets) if it.status != "done" and it.key not in exhausted)


def countries_message(n: int) -> str:
    return f"{n:,} Creatives still need Shown countries; rerun the same command to continue"
