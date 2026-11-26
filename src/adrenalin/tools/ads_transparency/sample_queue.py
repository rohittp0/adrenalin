"""Queue side of the Copy sample (ADR 0003): held Creatives, groups, weights and sampling rounds.

The pure rules (Messages, Coverage, buckets, allocation) are in `sampling.py`; this module applies them to a run's
`WorkQueue`. `held` is a queue stage no worker claims: listed Creatives left out of the sample so far.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import date

from adrenalin.common.pipeline import Item, WorkQueue
from adrenalin.tools.ads_transparency import sampling
from adrenalin.tools.ads_transparency.copy import MIN_CONFIDENCE, copy_status, shown_copy
from adrenalin.tools.ads_transparency.rpc import Query, preview_ids

log = logging.getLogger(__name__)

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


READ_STAGES = ("detail", "fetch", "script", "extract")
# an extract item's payload key: the result it had when it was queued to be read again (a stale content.js read),
# exported until the new read is done
PREVIOUS_RESULT = "previous_result"


def read_results(extracts: list[Item]) -> dict[str, dict | None]:
    """Each Variation's copy as exported, from the run's extract items: the result once done, else the result kept
    from before it was queued to be read again; store badges are blanked (`shown_copy`). Keys with neither are
    left out; the rest count as done."""
    out = {}
    for it in extracts:
        if it.status == "done":
            out[it.key] = shown_copy(it.result)
        elif PREVIOUS_RESULT in it.payload:
            out[it.key] = shown_copy(it.payload[PREVIOUS_RESULT])
    return out


def requeue_keeping_result(q: WorkQueue, keys: list[str]) -> None:
    """Requeue done extract items to be read again, keeping each one's result in its payload until the new read
    is done (`read_results`)."""
    for key in keys:
        it = q.item("extract", key)
        if it is not None and it.status == "done":
            q.put("extract", key, {**it.payload, PREVIOUS_RESULT: it.result}, update_payload=True)
    q.requeue("extract", keys)


def exhausted_reads(q: WorkQueue) -> set[str]:
    """Keys whose reading failed on every retry, in any reading stage: Variation keys (`creative#n`), and with
    `--variations` Creative IDs of failed detail items. No run retries them, so their copy is `unreadable`."""
    return set().union(*(q.exhausted(s) for s in READ_STAGES))


def row_status(extract_status: str | None, result: dict | None, fmt: str, key: str, exhausted: set[str],
               held: bool = False) -> str:
    """The Copy status of a Variation row (`key` is `creative#n`); the one rule for the CSV and the Copy sample."""
    failed = key in exhausted or key.partition("#")[0] in exhausted
    return copy_status(extract_status, {**(result or {}), "format": fmt}, held=held, exhausted=failed)


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
class SampleTally:
    """The run's Copy sample counted per Creative, from Variation 1 only (so `--variations` rows don't count twice)."""
    groups: dict[str, SampleGroup] = field(default_factory=dict)
    group_of: dict[str, str] = field(default_factory=dict)  # Creative -> group
    processed: set[str] = field(default_factory=set)  # Creatives whose Variation 1 copy reading finished
    representative: set[str] | None = None
    design_weights: dict[str, float] = field(default_factory=dict)
    message: dict[str, str] = field(default_factory=dict)  # processed Creative -> countable Message ("" if none)

    def weight(self, creative_id: str, sampled: bool) -> float | str:
        """A Creative's sample weight, for all its rows: its group's Inventory / Creatives read (1.0 on a full
        read), or "" while its Variation 1 is unread. Per group, the weights of Variation 1 rows sum to the
        Inventory."""
        if self.representative is not None:
            return self.design_weights.get(self.group_of[creative_id], "") if creative_id in self.representative else ""
        if creative_id not in self.processed:
            return ""
        if not sampled:
            return 1.0
        g = self.groups[self.group_of[creative_id]]
        return sampling.sample_weight(g.inventory, g.processed)


def _admission_rank(key: str) -> str:
    return hashlib.sha1(key.partition("#")[0].encode()).hexdigest()


def sample_groups(q: WorkQueue, variations: bool, query: Query, today: date) -> SampleTally:
    """Per-group Inventory, reads and Messages of the run, and each Creative's group and Message."""
    tally = SampleTally()
    extracted = read_results(q.items("extract"))
    exhausted = exhausted_reads(q)
    for it in q.items(listing_stage(variations)):
        cr = it.payload["creative_id"]
        if cr in tally.group_of:
            continue
        group = tally.group_of.setdefault(cr, sample_group(it.payload, query, today))
        g = tally.groups.setdefault(group, SampleGroup())
        g.admitted += 1
        key = f"{cr}#1"
        status = "done" if key in extracted else None
        result = extracted.get(key)
        if row_status(status, result, it.payload.get("format", ""), key, exhausted) != "pending":
            g.processed += 1
            tally.processed.add(cr)
            tally.message[cr] = message_of(status, result, it.payload.get("format", ""))
            g.messages.append(tally.message[cr])
    for it in sorted(q.items(HELD), key=lambda i: _admission_rank(i.key)):
        cr = it.payload["creative_id"]
        if cr in tally.group_of:
            continue  # already admitted (a stale held copy); never count a Creative twice
        group = tally.group_of.setdefault(cr, sample_group(it.payload, query, today))
        g = tally.groups.setdefault(group, SampleGroup())
        g.held.append(it.key)
    meta = sampling_meta(q)
    if meta.get("mode") == "stratified":
        cohort = meta.get("cohort", {})
        tally.representative = set(cohort.get("selected", []))
        tally.design_weights = cohort.get("weights", {})
    return tally


def seed_round(q: WorkQueue) -> None:
    """Queue the next sampling round unless one is already waiting. Round keys are never reused, since a done
    `round:N` can't be put again."""
    if q.backlog("sample") == 0:
        q.put("sample", f"round:{_rounds(q)}", {})


def _rounds(q: WorkQueue) -> int:
    return sum(n for (stage, _), n in q.counts().items() if stage == "sample")


def sampling_meta(q: WorkQueue) -> dict:
    """The `sampling` meta: admission probability `p` and the `estimate` it was sized from (written once), and the
    run's Coverage `target` and `max_ads`."""
    return json.loads(q.get_meta("sampling") or "{}")


@dataclass(frozen=True)
class RoundSettings:
    variations: bool
    query: Query
    today: date
    read_all: bool
    coverage: float
    max_ads: int | None
    sampling_mode: str = "novelty"


def round_lead(q: WorkQueue) -> int:
    """How many unread Creatives the next round may leave behind (the `sample` stage's `max_ahead`). After a novelty
    round the next one is chosen while up to one round is still being read, so novelty lags a round. Otherwise
    (after the head start, a minimums round, or during a relist) it waits until every admitted Creative is read."""
    if q.backlog("list"):
        return 0
    waiting = q.items("sample", "pending")
    return ROUND_BATCH if waiting and waiting[0].payload.get("novelty") else 0


def _in_reading(q: WorkQueue) -> set[str]:
    """Creatives with a read still queued or running."""
    return {i.key.partition("#")[0] for s in READ_STAGES for st in ("pending", "running") for i in q.items(s, st)}


def _judge(q: WorkQueue, rounds: list[list[str]], tally: SampleTally) -> tuple[str, list[list[str]]]:
    """Judge earlier novelty rounds by their processed Creatives. "stop": one round's reads are over and its
    processed Creatives gave no readable Message; "defer": one round's reads are over but none was processed (its
    reads wait for Codex or failed: retried on the next run); otherwise "go". Also returns the rounds still being
    read, judged by a later round."""
    open_rounds = [ids for ids in rounds if not any(tally.message.get(c) for c in ids)]
    if not open_rounds:
        return "go", []
    reading = _in_reading(q)
    carry = [ids for ids in open_rounds if any(c in reading for c in ids)]
    over = [ids for ids in open_rounds if ids not in carry]
    if any(c in tally.processed for ids in over for c in ids):
        return "stop", carry
    return ("defer" if over else "go"), carry


def _unjudged(payload: dict) -> list[list[str]]:
    if "unjudged" in payload:
        return payload["unjudged"]
    # a round queued before rounds overlapped: only the previous round, if it was a novelty round
    last = payload.get("admitted") or []
    return [last] if payload.get("novelty") and last else []


def stratified_round(q: WorkQueue, s: RoundSettings) -> dict:
    meta = sampling_meta(q)
    seed = meta.setdefault("seed", secrets.token_hex(16))
    grouped: dict[str, dict[str, str]] = {}
    for stage in (HELD, listing_stage(s.variations)):
        for it in q.items(stage):
            grouped.setdefault(sample_group(it.payload, s.query, s.today), {})[it.payload["creative_id"]] = it.key
    sizes = {k: len(v) for k, v in grouped.items()}
    quotas = sampling.stratified_allocation(sizes, s.max_ads or sampling.DEFAULT_SAMPLE_SIZE)
    selected = []
    chosen = []
    target = listing_stage(s.variations)
    for group, members in sorted(grouped.items()):
        ranked = sorted(members, key=lambda cr: hashlib.sha256(f"{seed}:{cr}".encode()).hexdigest())
        for cr in ranked[:quotas[group]]:
            selected.append(cr)
            key = members[cr]
            if not q.has(target, key):
                chosen.append(key)
    meta["cohort"] = {"selected": selected, "quotas": quotas,
                      "weights": {k: sizes[k] / quotas[k] for k in quotas if quotas[k]},
                      "inventory": sum(sizes.values())}
    q.set_meta("sampling", json.dumps(meta))  # persist design before admitting work
    q.move(HELD, target, chosen)
    log.info(f"sample: stratified design selects {len(selected):,} of {sum(sizes.values()):,} Creatives "
        f"across {len(sizes):,} groups; {len(chosen):,} additional reads. No Coverage stopping target.")
    return {"admitted": len(chosen), "selected": len(selected), "mode": "stratified"}


def run_round(q: WorkQueue, item: Item, s: RoundSettings) -> dict:
    """One sampling round (the `sample` stage's worker). It runs once the listing is done and the admitted Creatives
    have been read (all of them, or all but the last novelty round: see `round_lead`). Once every group has its
    minimum it stops at the Coverage target, at `max_ads`, when Coverage can't be estimated, or when a novelty round
    found no readable Message; otherwise it admits the next held Creatives and queues the next round (with the
    Creatives it admitted), before this one completes."""
    if not q.get_meta("list_done"):
        # The listing failed this run, so the Inventory is incomplete: list_page queues a round when it finishes.
        return {"deferred": True}
    if s.sampling_mode == "stratified" and not s.read_all:
        return stratified_round(q, s)
    tally = sample_groups(q, s.variations, s.query, s.today)
    groups = tally.groups
    read = sum(g.admitted for g in groups.values())
    inventory = sum(g.inventory for g in groups.values())
    cov = sampling.coverage([m for g in groups.values() for m in g.messages])
    stopped = {"admitted": 0, "read": read, "coverage": cov}
    novelty = False
    carry: list[list[str]] = []
    if s.read_all:
        chosen = [k for g in groups.values() for k in g.held]
    else:
        # Admitted Creatives count toward the minimum even if their read failed, so a failure can't make the
        # round admit everything; the failed item is retried on the next run.
        quotas_met = all(g.admitted >= min(sampling.MIN_PER_BUCKET, g.inventory) for g in groups.values())
        if sampling.should_stop(cov, read, s.coverage, s.max_ads, quotas_met):
            _log_stop(cov, s.coverage, read, inventory)
            return stopped
        if quotas_met and cov is None and len(tally.processed) >= min(sampling.HEAD_START, inventory):
            # Without countable Messages Coverage is never estimated, and reading on would read everything.
            log.info(f"sample: Coverage unknown: no readable Messages after {len(tally.processed):,} Creatives; stopping")
            return stopped
        verdict, carry = _judge(q, _unjudged(item.payload), tally) if quotas_met else ("go", [])
        if verdict == "stop":
            log.info(f"sample: a round's processed Creatives gave no readable Message (all failed or without copy); "
                f"stopping with Coverage {'unknown' if cov is None else f'{cov:.3f}'}")
            return stopped
        if verdict == "defer":
            log.info("sample: a round's Creatives are all still unread (Codex limit or failed reads); no more rounds "
                "this run, rerun to continue")
            return {**stopped, "deferred": True}
        if not quotas_met and s.max_ads is not None:
            floor = sum(min(sampling.MIN_PER_BUCKET, g.inventory) for g in groups.values())
            if floor > s.max_ads:
                log.warning(f"--max-ads {s.max_ads:,} is below the group minimums: every launch period x "
                    f"running/stopped x Ads account group gets up to {sampling.MIN_PER_BUCKET} reads, "
                    f"{floor:,} Creatives in all")
        # Minimums first, alone: novelty is judged only once every group has reads to judge it by.
        novelty = quotas_met
        batch = 0 if not quotas_met else ROUND_BATCH if s.max_ads is None else \
            max(0, min(ROUND_BATCH, s.max_ads - read))
        states = {k: sampling.BucketState(g.inventory, g.admitted, g.messages) for k, g in groups.items()}
        plan = sampling.allocate(states, {k: len(g.held) for k, g in groups.items()}, batch)
        chosen = [key for k, n in plan.items() for key in groups[k].held[:n]]
    if not chosen:
        _log_stop(cov, s.coverage, read, inventory)
        return stopped
    q.move(HELD, listing_stage(s.variations), chosen)
    admitted = [k.partition("#")[0] for k in chosen]
    held = sum(len(g.held) for g in groups.values()) - len(chosen)
    shown = "unknown" if cov is None else f"{cov:.3f}"
    log.info(f"sample: {item.key.replace(':', ' ')}: read {len(tally.processed):,}, Coverage {shown} "
        f"(target {s.coverage}), held {held:,}")
    # The next round goes in while this one still runs, so the stage never looks finished between rounds.
    q.put("sample", f"round:{_rounds(q)}",
          {"admitted": admitted, "novelty": novelty, "unjudged": carry + ([admitted] if novelty else [])})
    return {"admitted": len(chosen), "read": read, "coverage": cov}


def _log_stop(cov: float | None, target: float, read: int, inventory: int) -> None:
    shown = "unknown" if cov is None else f"{cov:.3f}"
    log.info(f"sample: stopping with Coverage {shown} (target {target}); {read:,} of {inventory:,} Creatives in the "
        "Copy sample")
