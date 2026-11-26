import io

from adrenalin.common.pipeline import WorkQueue
from adrenalin.common.progress import Bar, QueueProgress, stage_bar


def test_stage_bar_counts_finished_over_queued():
    c = {("A", "done"): 3, ("A", "failed"): 1, ("A", "pending"): 4, ("B", "done"): 9}
    assert stage_bar("A").count(c) == (4, 8)
    assert stage_bar("A", total=lambda c: None).count(c) == (4, None)


def test_queue_progress_follows_the_queue_and_writes_above_bars(tmp_path):
    q = WorkQueue(tmp_path / "q.sqlite3")
    for k in "abcd":
        q.put("A", k, {})
    q.claim("A")
    q.complete("A", "a")  # done before the bars start: counted as the initial value, not as this run's rate
    out = io.StringIO()
    p = QueueProgress([stage_bar("A", "stage a"), Bar("total", lambda c: (c.get(("A", "done"), 0), None))],
                      interval=60, file=out).start(q)
    assert [b.n for b in p._tqdm] == [1, 1] and p._tqdm[0].total == 4
    for k in "bc":
        q.claim("A")
        q.complete("A", k)
    q.put("A", "e", {})
    p.refresh()
    assert [b.n for b in p._tqdm] == [3, 3] and p._tqdm[0].total == 5 and p._tqdm[1].total is None
    p.write("warning: hello")
    p.close()
    text = out.getvalue()
    assert "warning: hello" in text and "stage a" in text and "3/5" in text
    assert p._thread is None
    q.close()


def test_run_progress_starts_and_closes_or_does_nothing(tmp_path):
    from adrenalin.common.progress import run_progress
    q = WorkQueue(tmp_path / "q.sqlite3")
    with run_progress(q, [stage_bar("A")], enabled=False) as p:
        assert p is None
    with run_progress(q, [stage_bar("A")], enabled=True) as p:
        assert isinstance(p, QueueProgress) and p._thread is not None
    assert p._thread is None and p._tqdm == []
