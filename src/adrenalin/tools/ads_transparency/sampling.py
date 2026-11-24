"""Copy sample core: Messages, Coverage, buckets and allocation (pure functions).

Large Advertisers are read as a Copy sample: random admission (deterministic per
Creative), a minimum quota per launch period x running/stopped bucket, then
novelty-driven allocation until Good-Turing Coverage reaches the target.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

FULL_READ_LIMIT = 2000
HEAD_START = 2000
DEFAULT_SAMPLE_SIZE = 2000
MIN_PER_BUCKET = 3
RUNNING_SLACK_DAYS = 2
CHUNK = 25

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
_KEEP_SYMBOLS = set("%$€£₹")
_SEPARATOR = " || "


def _normalize_part(text: str) -> str:
    text = text.translate(_QUOTES).casefold()
    kept = "".join(ch if (ch.isalnum() or ch in _KEEP_SYMBOLS) else " " for ch in text)
    return re.sub(r"\s+", " ", kept).strip()


def normalize_message(headline: str, description: str) -> str:
    """Message key of a Creative's copy; "" when both parts are blank."""
    parts = (_normalize_part(headline or ""), _normalize_part(description or ""))
    if not any(parts):
        return ""
    return _SEPARATOR.join(parts)


def coverage(messages: Sequence[str]) -> float | None:
    """Good-Turing Coverage: 1 - singletons / n over non-empty Messages."""
    counts = Counter(m for m in messages if m)
    n = sum(counts.values())
    if n == 0:
        return None
    singletons = sum(1 for c in counts.values() if c == 1)
    return 1 - singletons / n


def admit_probability(estimate: int | None, read_all: bool = False, max_ads: int | None = None) -> float:
    """Head-start admission probability: about HEAD_START Creatives (or `max_ads`, if fewer) of the estimate.
    `max_ads` below the estimate overrides the full read of a small Inventory; `read_all` wins over everything, and
    without an estimate every Creative is admitted."""
    if read_all or estimate is None:
        return 1.0
    if max_ads is not None and estimate > max_ads:
        return min(HEAD_START, max_ads) / estimate
    if estimate <= FULL_READ_LIMIT:
        return 1.0
    return HEAD_START / estimate


def admitted(creative_id: str, p: float) -> bool:
    digest = hashlib.sha1(creative_id.encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF < p


def bucket_key(
    first_shown: date,
    last_shown: date,
    start: date | None,
    end: date | None,
    today: date,
) -> str:
    if start is not None and first_shown >= start:
        iso = first_shown.isocalendar()
        period = f"{iso.year}-W{iso.week:02d}"
    else:
        period = f"{first_shown.year}-{first_shown.month:02d}"
    running = last_shown >= (end or today) - timedelta(days=RUNNING_SLACK_DAYS)
    return f"{period}|{'running' if running else 'stopped'}"


@dataclass
class BucketState:
    inventory: int
    read: int
    messages: list[str]


def _score(singletons: int, read: int) -> float:
    return (singletons + 1) / (read + 2)


def allocate(
    buckets: Mapping[str, BucketState],
    available: Mapping[str, int],
    batch: int = 50,
) -> dict[str, int]:
    """How many held Creatives to read next from each bucket (only counts > 0).

    Minimum quotas are filled first and win over `batch`. The rest of the batch
    goes out in chunks to the buckets with the most novelty per read.
    """
    out: dict[str, int] = {}
    avail = {k: max(0, available.get(k, 0)) for k in buckets}
    reads = {k: b.read for k, b in buckets.items()}

    for key, b in buckets.items():
        quota = max(0, min(MIN_PER_BUCKET, b.inventory) - b.read)
        take = min(quota, avail[key])
        if take > 0:
            out[key] = take
            avail[key] -= take
            reads[key] += take

    remaining = batch - sum(out.values())
    singletons = {
        key: sum(1 for c in Counter(m for m in b.messages if m).values() if c == 1)
        for key, b in buckets.items()
    }
    while remaining > 0:
        candidates = [k for k in buckets if avail[k] > 0]
        if not candidates:
            break
        best = max(candidates, key=lambda k: _score(singletons[k], reads[k]))
        take = min(CHUNK, remaining, avail[best])
        out[best] = out.get(best, 0) + take
        avail[best] -= take
        reads[best] += take
        remaining -= take
    return out


def should_stop(
    cov: float | None,
    read: int,
    target: float,
    max_ads: int | None,
    quotas_met: bool,
) -> bool:
    if not quotas_met:
        return False
    if max_ads is not None and read >= max_ads:
        return True
    return cov is not None and cov >= target


def sample_weight(inventory: int, read: int) -> float:
    if read <= 0:
        raise ValueError("read must be positive")
    return inventory / read


def stratified_allocation(inventory: Mapping[str, int], target: int) -> dict[str, int]:
    """Minimum representation, then proportional allocation of remaining slots.

    Minimums may exceed target. Allocation is deterministic and never exceeds a
    stratum's Inventory. It depends on Inventory only, never observed copy.
    """
    quotas = {k: min(MIN_PER_BUCKET, n) for k, n in sorted(inventory.items())}
    remaining = min(sum(inventory.values()), max(target, sum(quotas.values()))) - sum(quotas.values())
    while remaining:
        active = {k: n for k, n in inventory.items() if quotas[k] < n}
        total = sum(active.values())
        shares = {k: remaining * n / total for k, n in active.items()}
        added = 0
        for k, share in shares.items():
            take = min(inventory[k] - quotas[k], int(share))
            quotas[k] += take
            added += take
        remaining -= added
        for k in sorted(active, key=lambda k: (-(shares[k] % 1), k)):
            if remaining and quotas[k] < inventory[k]:
                quotas[k] += 1
                remaining -= 1
    return quotas
