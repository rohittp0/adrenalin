"""Shown countries of the weighted Copy sample: which Creatives get them, and the `countries` stage's queue items."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date

from adrenalin.common.pipeline import WorkQueue
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.rpc import Query
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
