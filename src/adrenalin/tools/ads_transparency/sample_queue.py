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
    processed: int = 0  # Creatives whose copy reading finished (Variation 1 neither pending nor not_sampled)
    messages: list[str] = field(default_factory=list)  # one per processed Creative (Variation 1); "" if none

    @property
    def inventory(self) -> int:
        return self.admitted + len(self.held)


@dataclass
class SampleAccount:
    """The run's Copy sample counted per Creative, from Variation 1 only (so `--variations` rows don't count twice)."""
    groups: dict[str, SampleGroup] = field(default_factory=dict)
    group_of: dict[str, str] = field(default_factory=dict)  # Creative -> group
    processed: set[str] = field(default_factory=set)  # Creatives whose Variation 1 copy reading finished
    message: dict[str, str] = field(default_factory=dict)  # processed Creative -> countable Message ("" if none)

    def weight(self, creative_id: str, sampled: bool) -> float | str:
        """A Creative's sample weight, for all its rows: its group's Inventory / Creatives read (1.0 on a full
        read), or "" while its Variation 1 is unread. Per group, the weights of Variation 1 rows sum to the
        Inventory."""
        if creative_id not in self.processed:
            return ""
        if not sampled:
            return 1.0
        g = self.groups[self.group_of[creative_id]]
        return sampling.sample_weight(g.inventory, g.processed)


def _admission_rank(key: str) -> str:
    return hashlib.sha1(key.partition("#")[0].encode()).hexdigest()


def sample_groups(q: WorkQueue, variations: bool, query: Query, today: date) -> SampleAccount:
    """Per-group Inventory, reads and Messages of the run, and each Creative's group and Message."""
    acc = SampleAccount()
    extracted = dict(q.results("extract"))
    for it in q.items(listing_stage(variations)):
        cr = it.payload["creative_id"]
        if cr in acc.group_of:
            continue
        g = acc.groups.setdefault(acc.group_of.setdefault(cr, sample_group(it.payload, query, today)), SampleGroup())
        g.admitted += 1
        key = f"{cr}#1"
        status = "done" if key in extracted else None
        result = extracted.get(key)
        if copy_status(status, {**(result or {}), "format": it.payload.get("format", "")}) != "pending":
            g.processed += 1
            acc.processed.add(cr)
            acc.message[cr] = message_of(status, result, it.payload.get("format", ""))
            g.messages.append(acc.message[cr])
    for it in sorted(q.items(HELD), key=lambda i: _admission_rank(i.key)):
        cr = it.payload["creative_id"]
        if cr in acc.group_of:
            continue  # already admitted (a stale held copy); never count a Creative twice
        g = acc.groups.setdefault(acc.group_of.setdefault(cr, sample_group(it.payload, query, today)), SampleGroup())
        g.held.append(it.key)
    return acc


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
    """One sampling round (the `sample` stage's worker). It runs once the listing is done and every admitted
    Creative has been read. Once every group has its minimum it stops at the Coverage target, at `max_ads`, or when
    Coverage can't be estimated; otherwise it admits the next held Creatives and queues the next round (with the
    Creatives it admitted), before this one completes."""
    if not q.get_meta("list_done"):
        # The listing failed this run, so the Inventory is incomplete: list_page queues a round when it finishes.
        return {"deferred": True}
    acc = sample_groups(q, s.variations, s.query, s.today)
    groups = acc.groups
    read = sum(g.admitted for g in groups.values())
    inventory = sum(g.inventory for g in groups.values())
    cov = sampling.coverage([m for g in groups.values() for m in g.messages])
    stopped = {"admitted": 0, "read": read, "coverage": cov}
    novelty = False
    if s.read_all:
        chosen = [k for g in groups.values() for k in g.held]
    else:
        # Admitted Creatives count toward the minimum even if their read failed, so a failure can't make the
        # round admit everything; the failed item is retried on the next run.
        quotas_met = all(g.admitted >= min(sampling.MIN_PER_BUCKET, g.inventory) for g in groups.values())
        if sampling.should_stop(cov, read, s.coverage, s.max_ads, quotas_met):
            _log_stop(log, cov, s.coverage, read, inventory)
            return stopped
        if quotas_met and cov is None and len(acc.processed) >= min(sampling.HEAD_START, inventory):
            # Without countable Messages Coverage is never estimated, and reading on would read everything.
            log(f"sample: Coverage unknown: no readable Messages after {len(acc.processed):,} Creatives; stopping")
            return stopped
        last = item.payload.get("admitted") or []
        if quotas_met and item.payload.get("novelty") and not any(acc.message.get(c) for c in last):
            log(f"sample: the last round's {len(last):,} Creatives gave no readable Message (all failed or without "
                f"copy); stopping with Coverage {'unknown' if cov is None else f'{cov:.3f}'}")
            return stopped
        # Minimums first, alone: novelty is judged only once every group has reads to judge it by.
        novelty = quotas_met
        batch = 0 if not quotas_met else ROUND_BATCH if s.max_ads is None else \
            max(0, min(ROUND_BATCH, s.max_ads - read))
        states = {k: sampling.BucketState(g.inventory, g.admitted, g.messages) for k, g in groups.items()}
        plan = sampling.allocate(states, {k: len(g.held) for k, g in groups.items()}, batch)
        chosen = [key for k, n in plan.items() for key in groups[k].held[:n]]
    if not chosen:
        _log_stop(log, cov, s.coverage, read, inventory)
        return stopped
    q.move(HELD, listing_stage(s.variations), chosen)
    # The next round goes in while this one still runs, so the stage never looks finished between rounds.
    q.put("sample", f"round:{_rounds(q)}",
          {"admitted": [k.partition("#")[0] for k in chosen], "novelty": novelty})
    return {"admitted": len(chosen), "read": read, "coverage": cov}


def _log_stop(log: Callable[[str], None], cov: float | None, target: float, read: int, inventory: int) -> None:
    shown = "unknown" if cov is None else f"{cov:.3f}"
    log(f"sample: stopping with Coverage {shown} (target {target}); {read:,} of {inventory:,} Creatives in the "
        "Copy sample")
