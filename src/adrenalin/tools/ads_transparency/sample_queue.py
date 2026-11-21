"""Queue side of the Copy sample (ADR 0003): held Creatives, groups, weights and sampling rounds.

The pure rules (Messages, Coverage, buckets, allocation) are in `sampling.py`; this module applies them to a run's
`WorkQueue`. `held` is a queue stage no worker claims: listed Creatives left out of the sample so far.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from adrenalin.common.pipeline import Item, WorkQueue
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.copy import MIN_CONFIDENCE, copy_status
from adrenalin.tools.ads_transparency.rpc import Query, preview_ids

HELD = "held"
ROUND_BATCH = 50  # held Creatives a sampling round admits by novelty, once every group has its minimum


def listing_stage(variations: bool) -> str:
    """Where an admitted Creative goes: `detail` lists its Variations first, otherwise straight to `fetch`."""
    return "detail" if variations else "fetch"


def listing_key(creative_id: str, variations: bool) -> str:
    """A Creative's key in `held` and in its listing stage (the same, so `WorkQueue.move` can admit it)."""
    return creative_id if variations else f"{creative_id}#1"


def sample_group(payload: dict, query: Query, today: date) -> str:
    """The Copy sample's allocation unit: launch period x running/stopped bucket, and the Ads account that the
    listed Preview exposes ("" when it exposes none). `payload` is a listing payload (held, detail or fetch)."""
    p = payload.get("preview") or {}
    account = preview_ids(p.get("url", ""))[0] if p.get("kind") == "content_js" else ""
    bucket = sampling.bucket_key(date.fromisoformat(payload["first_shown"]),
                                 date.fromisoformat(payload["last_shown"]), query.start, query.end, today)
    return f"{bucket}|{account}"


def message_of(extract_status: str | None, result: dict | None, fmt: str) -> str:
    """The Message a read counts toward Coverage; "" unless the copy was read with enough confidence."""
    r = result or {}
    if copy_status(extract_status, {**r, "format": fmt}) != "read":
        return ""
    conf = r.get("confidence")
    if conf is not None and conf < MIN_CONFIDENCE:
        return ""  # OCR misreads would look like new Messages
    return sampling.normalize_message(r.get("headline") or "", r.get("description") or "")


@dataclass
class SampleGroup:
    held: list[str] = field(default_factory=list)  # held keys, in admission-hash order
    admitted: int = 0  # Creatives sent to reading (read, pending or failed)
    processed: int = 0  # Creatives whose copy reading finished (copy status neither pending nor not_sampled)
    messages: list[str] = field(default_factory=list)  # one per processed Creative (Variation 1); "" if none

    @property
    def inventory(self) -> int:
        return self.admitted + len(self.held)


def _admission_rank(key: str) -> str:
    return hashlib.sha1(key.partition("#")[0].encode()).hexdigest()


def sample_groups(q: WorkQueue, variations: bool, query: Query, today: date) -> tuple[dict[str, SampleGroup],
                                                                                     dict[str, str]]:
    """Per-group Inventory, reads and Messages of the run, counted per Creative (Variation 1 only), and each
    Creative's group."""
    groups: dict[str, SampleGroup] = {}
    of_creative: dict[str, str] = {}
    extracted = dict(q.results("extract"))
    for it in q.items(listing_stage(variations)):
        cr = it.payload["creative_id"]
        if cr in of_creative:
            continue
        g = groups.setdefault(of_creative.setdefault(cr, sample_group(it.payload, query, today)), SampleGroup())
        g.admitted += 1
        key = f"{cr}#1"
        status = "done" if key in extracted else None
        result = extracted.get(key)
        if copy_status(status, {**(result or {}), "format": it.payload.get("format", "")}) != "pending":
            g.processed += 1
            g.messages.append(message_of(status, result, it.payload.get("format", "")))
    for it in sorted(q.items(HELD), key=lambda i: _admission_rank(i.key)):
        cr = it.payload["creative_id"]
        if cr in of_creative:
            continue  # already admitted (a stale held copy); never count a Creative twice
        g = groups.setdefault(of_creative.setdefault(cr, sample_group(it.payload, query, today)), SampleGroup())
        g.held.append(it.key)
    return groups, of_creative


def seed_round(q: WorkQueue) -> None:
    """Queue the next sampling round unless one is already waiting. Round keys are never reused, since a done
    `round:N` can't be put again."""
    if q.backlog("sample") == 0:
        q.put("sample", f"round:{_rounds(q)}", {})


def _rounds(q: WorkQueue) -> int:
    return sum(n for (stage, _), n in q.counts().items() if stage == "sample")


def sampling_meta(q: WorkQueue) -> dict:
    """The `sampling` meta: admission probability `p` (written once), and the run's Coverage `target` and
    `max_ads`."""
    return json.loads(q.get_meta("sampling") or "{}")


@dataclass(frozen=True)
class RoundSettings:
    variations: bool
    query: Query
    today: date
    read_all: bool
    coverage: float
    max_ads: int | None


def run_round(q: WorkQueue, item: Item, s: RoundSettings, log: Callable[[str], None]) -> dict:
    """One sampling round (the `sample` stage's worker). It runs once every admitted Creative has been read: it
    stops at the Coverage target (or `max_ads`) once every group has its minimum, otherwise it admits the next
    held Creatives and queues the next round, before this one completes."""
    if not q.get_meta("list_done"):
        # A relist is under way: the Inventory is incomplete. list_page queues a new round when it finishes.
        return {"deferred": True}
    groups, _ = sample_groups(q, s.variations, s.query, s.today)
    read = sum(g.admitted for g in groups.values())
    inventory = sum(g.inventory for g in groups.values())
    cov = sampling.coverage([m for g in groups.values() for m in g.messages])
    if s.read_all:
        chosen = [k for g in groups.values() for k in g.held]
    else:
        # Admitted Creatives count toward the minimum even if their read failed, so a failure can't make the
        # round admit everything; the failed item is retried on the next run.
        quotas_met = all(g.admitted >= min(sampling.MIN_PER_BUCKET, g.inventory) for g in groups.values())
        if sampling.should_stop(cov, read, s.coverage, s.max_ads, quotas_met):
            _log_stop(log, cov, s.coverage, read, inventory)
            return {"admitted": 0, "read": read, "coverage": cov}
        # Minimums first, alone: novelty is judged only once every group has reads to judge it by.
        batch = 0 if not quotas_met else ROUND_BATCH if s.max_ads is None else \
            max(0, min(ROUND_BATCH, s.max_ads - read))
        states = {k: sampling.BucketState(g.inventory, g.admitted, g.messages) for k, g in groups.items()}
        plan = sampling.allocate(states, {k: len(g.held) for k, g in groups.items()}, batch)
        chosen = [key for k, n in plan.items() for key in groups[k].held[:n]]
    if not chosen:
        _log_stop(log, cov, s.coverage, read, inventory)
        return {"admitted": 0, "read": read, "coverage": cov}
    q.move(HELD, listing_stage(s.variations), chosen)
    # The next round goes in while this one still runs, so the stage never looks finished between rounds.
    q.put("sample", f"round:{_rounds(q)}", {})
    return {"admitted": len(chosen), "read": read, "coverage": cov}


def _log_stop(log: Callable[[str], None], cov: float | None, target: float, read: int, inventory: int) -> None:
    shown = "unknown" if cov is None else f"{cov:.3f}"
    log(f"sample: stopping with Coverage {shown} (target {target}); {read:,} of {inventory:,} Creatives in the "
        "Copy sample")
