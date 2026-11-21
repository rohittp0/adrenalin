import threading
import time

import pytest

from adrenalin.common.http import Blocked
from adrenalin.common.pipeline import Pipeline, Stage, WorkQueue


def quiet(_msg):
    pass


def test_put_is_idempotent_and_claim_marks_running(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    assert q.put("A", "1", {"x": 1}) is True
    assert q.put("A", "1", {"x": 2}) is False
    assert q.backlog("A") == 1
    item = q.claim("A")
    assert item.key == "1" and item.payload == {"x": 1} and item.status == "running"
    assert q.claim("A") is None
    assert q.backlog("A") == 1
    q.complete("A", "1", {"ok": True})
    assert q.backlog("A") == 0
    assert q.items("A", "done")[0].result == {"ok": True}
    q.close()


def test_reopen_resets_running_to_pending(tmp_path):
    p = tmp_path / "q.sqlite3"
    q = WorkQueue(p)
    q.put("A", "1", {})
    assert q.claim("A") is not None
    q.close()
    q = WorkQueue(p)
    assert q.backlog("A") == 1
    assert q.claim("A").key == "1"
    q.close()


def test_failed_items_retry_on_reopen_until_max_attempts(tmp_path):
    p = tmp_path / "q.sqlite3"
    q = WorkQueue(p)
    q.put("A", "1", {})
    for _ in range(3):
        assert q.claim("A") is not None
        q.fail("A", "1", "boom")
        q.close()
        q = WorkQueue(p)
    assert q.claim("A") is None
    assert q.items("A", "failed")[0].error == "boom"
    q.close()


def test_put_update_payload_keeps_result(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "1", {"v": 1})
    q.claim("A")
    q.complete("A", "1", {"r": 1})
    assert q.put("A", "1", {"v": 2}, update_payload=True) is False
    (it,) = q.items("A")
    assert it.payload == {"v": 2} and it.result == {"r": 1} and it.status == "done"
    q.close()


def test_requeue_and_meta(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "1", {})
    q.claim("A")
    q.complete("A", "1", {"r": 1})
    q.requeue("A", ["1"])
    (it,) = q.items("A")
    assert it.status == "pending" and it.result is None
    assert q.get_meta("k") is None
    q.set_meta("k", "v1")
    q.set_meta("k", "v2")
    assert q.get_meta("k") == "v2"
    q.close()


def _run(q, stages):
    baseline = threading.active_count()
    result = Pipeline(q, stages, poll=0.01, log=quiet).run()
    assert threading.active_count() == baseline
    return result


def test_stage_seconds_reported(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "seed", {})
    now = [0.0]

    def a(item, queue):
        now[0] += 2.0
        queue.put("B", "1", {})

    def b(item, queue):
        now[0] += 3.0

    result = Pipeline(q, [Stage("A", a), Stage("B", b)], poll=0.01, log=quiet, clock=lambda: now[0]).run()
    assert set(result.stage_seconds) == {"A", "B"}
    assert result.stage_seconds["A"]["busy"] > 0 and result.stage_seconds["B"]["busy"] > 0
    assert all("waiting" in v for v in result.stage_seconds.values())
    q.close()


def test_items_flow_through_stages_in_order(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "seed", {})

    def a(item, queue):
        for i in range(5):
            queue.put("B", str(i), {"i": i})

    result = _run(q, [Stage("A", a), Stage("B", lambda item, queue: {"ok": item.payload["i"]})])
    assert result.failed == {} and result.blocked is None
    assert len(q.items("B", "done")) == 5
    q.close()


def test_backpressure_caps_upstream_lead(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for i in range(20):
        q.put("A", str(i), {})
    seen = []

    def a(item, queue):
        queue.put("B", item.key, {})

    def b(item, queue):
        seen.append(queue.backlog("B"))
        time.sleep(0.01)

    sa = Stage("A", a, throttle_on=("B",), max_ahead=3)
    _run(q, [sa, Stage("B", b)])
    assert len(q.items("B", "done")) == 20
    assert max(seen) <= 3 + sa.concurrency
    q.close()


def test_blocked_stops_stage_but_downstream_drains(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for i in range(6):
        q.put("A", str(i), {})
    n = []

    def a(item, queue):
        n.append(1)
        if len(n) == 3:
            raise Blocked("h", 123.0)
        queue.put("B", item.key, {})

    result = _run(q, [Stage("A", a), Stage("B", lambda i, qu: None)])
    assert result.blocked is not None and result.blocked.host == "h"
    assert q.backlog("A") > 0
    assert len(q.items("B", "done")) == 2 == len(q.items("B"))
    q.close()


def test_worker_exception_marks_item_failed_and_continues(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "seed", {})

    def a(item, queue):
        for i in range(3):
            queue.put("B", str(i), {})

    def b(item, queue):
        if item.key == "1":
            raise ValueError("bad item")

    result = _run(q, [Stage("A", a), Stage("B", b)])
    assert result.failed == {"B": 1}
    assert "bad item" in q.items("B", "failed")[0].error
    assert len(q.items("B", "done")) == 2
    q.close()


def test_concurrency_runs_workers_in_parallel(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for i in range(2):
        q.put("A", str(i), {})
    barrier = threading.Barrier(2)

    def a(item, queue):
        barrier.wait(timeout=5)

    result = _run(q, [Stage("A", a, concurrency=2)])
    assert result.failed == {}
    assert len(q.items("A", "done")) == 2
    q.close()


def test_requeue_resets_attempts(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "1", {})
    q.claim("A")
    q.fail("A", "1", "x")
    q.requeue("A", ["1"])
    q.claim("A")
    q.fail("A", "1", "x")
    q.close()
    q = WorkQueue(tmp_path / "q.sqlite3", max_attempts=2)  # attempts==1 after requeue+claim
    assert q.items("A", "pending") and not q.items("A", "failed")
    q.close()


def test_pipeline_rejects_bad_config(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    with pytest.raises(ValueError):
        Pipeline(q, [Stage("A", lambda i, qu: None), Stage("A", lambda i, qu: None)])
    with pytest.raises(ValueError):
        Pipeline(q, [Stage("A", lambda i, qu: None, throttle_on=("Z",))])
    q.close()


def test_stage_loop_crash_is_recorded(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "1", {})
    real = q.claim
    calls = []

    def flaky(stage):
        if stage == "A" and not calls:
            calls.append(1)
            raise RuntimeError("db gone")
        return real(stage)

    q.claim = flaky
    result = _run(q, [Stage("A", lambda i, qu: None), Stage("B", lambda i, qu: None)])
    assert len(result.errors) == 1 and "db gone" in result.errors[0]
    q.close()


def test_blocked_downstream_does_not_hang_throttled_upstream(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for i in range(10):
        q.put("A", str(i), {})

    def b(item, queue):
        raise Blocked("h", 1.0)

    result = _run(q, [Stage("A", lambda i, qu: qu.put("B", i.key, {}) and None,
                            throttle_on=("B",), max_ahead=1), Stage("B", b)])
    assert result.blocked is not None
    # A stops instead of racing ahead of the blocked stage; its items wait for the next run
    assert q.backlog("A") >= 8


def test_blocked_stage_stops_its_producers_transitively_but_not_unrelated_stages(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for i in range(10):
        q.put("A", str(i), {})
    for i in range(5):
        q.put("C", str(i), {})
    logs = []

    def c(item, queue):
        raise Blocked("h", 1.0)

    stages = [
        Stage("A", lambda i, qu: qu.put("B", i.key, {}) and None, throttle_on=("B", "C"), max_ahead=100),
        Stage("B", lambda i, qu: time.sleep(0.01), throttle_on=("C",), max_ahead=100),
        Stage("C", c),
        Stage("D", lambda i, qu: None),
    ]
    for i in range(4):
        q.put("D", str(i), {})
    result = Pipeline(q, stages, poll=0.01, log=logs.append).run()
    assert result.blocked is not None
    assert any(m.startswith("B: stopping") for m in logs) and any(m.startswith("A: stopping") for m in logs)
    assert q.backlog("A") > 0 and q.backlog("C") == 5
    assert len(q.items("D", "done")) == 4  # a stage that doesn't wait on C keeps going
    q.close()


def test_stage_status_index_and_light_reads(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for k in "abc":
        q.put("A", k, {"big": "x" * 1000})
    q.claim("A")
    q.complete("A", "a", {"r": 1})
    q.claim("A")
    q.fail("A", "b", "boom")
    assert q.results("A") == [("a", {"r": 1})]
    assert q.results("A", "pending") == [("c", None)]
    assert (q.count("A", "done"), q.count("A", "failed"), q.count("A", "pending"), q.count("B", "done")) == (1, 1, 1, 0)
    plan = q._db.execute("EXPLAIN QUERY PLAN SELECT id FROM items WHERE stage='A' AND status='pending' "
                         "ORDER BY id LIMIT 1").fetchall()
    assert any("items_stage_status" in str(row) for row in plan)
    q.close()


def test_on_done_runs_after_complete_and_its_errors_do_not_fail_the_item(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for k in ("1", "2"):
        q.put("A", k, {})
    seen, logs = [], []

    def on_done(item, result):
        assert q.count("A", "done") >= 1  # already completed
        seen.append((item.key, result))
        if item.key == "2":
            raise OSError("cleanup failed")

    res = Pipeline(q, [Stage("A", lambda item, q: {"k": item.key}, on_done=on_done)], poll=0.01,
                   log=logs.append).run()
    assert sorted(seen) == [("1", {"k": "1"}), ("2", {"k": "2"})]
    assert res.failed == {} and q.count("A", "done") == 2
    assert any("cleanup failed" in m for m in logs)
    q.close()


def test_move_changes_stage_and_resets_status(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("held", "1", {"v": 1})
    q.put("held", "2", {"v": 2})
    q.put("held", "3", {"v": 3})
    q.put("fetch", "3", {"v": 30})
    q.move("held", "fetch", ["1", "3", "missing"])
    assert [i.key for i in q.items("held")] == ["2"]
    got = {i.key: i for i in q.items("fetch")}
    assert got["1"].payload == {"v": 1} and got["1"].status == "pending"
    assert got["3"].payload == {"v": 30}  # the target's item wins; the source copy is dropped
    assert q.backlog("fetch") == 2 and q.claim("fetch").key == "1"
    q.close()


def test_has(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    q.put("A", "1", {})
    assert q.has("A", "1") and not q.has("A", "2") and not q.has("B", "1")
    q.close()


def test_exhausted_lists_failed_items_out_of_attempts(tmp_path):
    p = tmp_path / "q.sqlite3"
    q = WorkQueue(p, max_attempts=2)
    q.put("A", "1", {})
    q.put("A", "2", {})
    for _ in range(2):
        q.claim("A")
        q.fail("A", "1", "boom")
        assert q.claim("A").key == "2"
        q.release("A", "2")
        q.close()
        q = WorkQueue(p, max_attempts=2)
    q.claim("A")
    q.fail("A", "2", "boom")  # failed once: retried on the next run
    assert q.exhausted("A") == {"1"}
    assert [i.attempts for i in q.items("A")] == [2, 1]
    q.close()


def test_max_ahead_can_be_decided_per_claim(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for i in range(10):
        q.put("A", str(i), {})
    limit = {"n": 0}
    seen = []

    def a(item, queue):
        queue.put("B", item.key, {})

    def b(item, queue):
        seen.append(queue.backlog("B"))
        time.sleep(0.01)

    _run(q, [Stage("A", a, throttle_on=("B",), max_ahead=lambda: limit["n"]), Stage("B", b)])
    assert len(q.items("B", "done")) == 10 and max(seen) <= 1
    q.close()
