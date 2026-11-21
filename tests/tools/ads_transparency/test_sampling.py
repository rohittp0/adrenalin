from datetime import date

import pytest

from adrenalin.tools.ads_transparency.sampling import (
    BucketState,
    admit_probability,
    admitted,
    allocate,
    bucket_key,
    coverage,
    normalize_message,
    sample_weight,
    should_stop,
)


def test_normalize_message():
    assert normalize_message("70% Off Hotels!", "Book now.") == normalize_message(
        "70% off hotels", "book  now"
    )
    assert normalize_message("That’s", "x") == normalize_message("That's", "x")
    assert normalize_message("", "") == ""
    assert normalize_message("Hi", "there") == "hi || there"
    assert "%" in normalize_message("70%", "")


def test_coverage_good_turing():
    assert coverage(["a", "a", "b", "c"]) == 0.5
    assert coverage([]) is None
    assert coverage(["", ""]) is None
    assert coverage(["a", "", "a", "b", "c", ""]) == 0.5


def test_admit_probability():
    assert admit_probability(None) == 1.0
    assert admit_probability(1500) == 1.0
    assert admit_probability(2000) == 1.0
    assert admit_probability(80000) == pytest.approx(0.025)
    assert admit_probability(80000, read_all=True) == 1.0


def test_admitted_is_deterministic_and_proportional():
    assert admitted("CR123", 0.5) == admitted("CR123", 0.5)
    hits = sum(admitted(f"CR{i:08d}", 0.1) for i in range(20000))
    assert 0.09 <= hits / 20000 <= 0.11
    assert admitted("CR1", 1.0)


def test_bucket_key():
    s, e, t = date(2026, 10, 20), date(2026, 11, 18), date(2026, 11, 21)
    assert bucket_key(date(2026, 11, 3), date(2026, 11, 17), s, e, t) == "2026-W45|running"
    assert bucket_key(date(2025, 3, 2), date(2026, 11, 10), s, e, t) == "2025-03|stopped"
    assert bucket_key(date(2026, 11, 3), date(2026, 11, 17), None, None, t) == "2026-11|stopped"
    assert bucket_key(date(2026, 11, 3), date(2026, 11, 20), None, None, t).endswith("|running")


def test_allocate_fills_min_quota_first():
    out = allocate(
        {"a": BucketState(10, 0, []), "b": BucketState(2, 0, [])},
        {"a": 10, "b": 2},
        batch=5,
    )
    assert out["a"] >= 3
    assert out["b"] == 2


def test_allocate_prefers_novel_buckets():
    out = allocate(
        {
            "novel": BucketState(1000, 10, [f"m{i}" for i in range(10)]),
            "same": BucketState(1000, 10, ["m"] * 10),
        },
        {"novel": 500, "same": 500},
        batch=25,
    )
    assert out.get("novel", 0) > out.get("same", 0)


def test_allocate_never_exceeds_available():
    out = allocate(
        {"a": BucketState(100, 0, []), "b": BucketState(100, 0, [])},
        {"a": 4, "b": 1},
        batch=50,
    )
    assert out["a"] <= 4 and out["b"] <= 1
    assert all(v > 0 for v in out.values())
    assert allocate({"a": BucketState(5, 5, [])}, {"a": 0}) == {}


def test_should_stop():
    assert not should_stop(0.99, 10, 0.95, None, quotas_met=False)
    assert should_stop(0.96, 10, 0.95, None, quotas_met=True)
    assert should_stop(0.5, 100, 0.95, 100, quotas_met=True)
    assert should_stop(None, 100, 0.95, 100, quotas_met=True)
    assert not should_stop(None, 10, 0.95, 100, quotas_met=True)


def test_sample_weight():
    assert sample_weight(100, 4) == 25.0
    with pytest.raises(ValueError):
        sample_weight(100, 0)


def test_admit_probability_caps_head_start_at_max_ads():
    assert admit_probability(80000, max_ads=500) == pytest.approx(500 / 80000)
    assert admit_probability(80000, max_ads=5000) == pytest.approx(0.025)
    assert admit_probability(1500, max_ads=5) == pytest.approx(5 / 1500)  # --max-ads overrides the full read
    assert admit_probability(1500, max_ads=1500) == 1.0
    assert admit_probability(None, max_ads=5) == 1.0
    assert admit_probability(80000, read_all=True, max_ads=5) == 1.0
