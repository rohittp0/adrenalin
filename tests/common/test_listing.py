import json

import pytest

import threading

from adrenalin.common.listing import (
    FirstClaim,
    PaginationLoop,
    page_key,
    put_page,
    record_cursor,
    restart_expired_cursor,
    restart_listing,
)
from adrenalin.common.pipeline import WorkQueue


@pytest.fixture
def q(tmp_path):
    queue = WorkQueue(tmp_path / "q.sqlite3")
    yield queue
    queue.close()


def test_page_key():
    assert page_key(None) == "page:" and page_key("") == "page:" and page_key("CUR1") == "page:CUR1"


def test_put_page_queues_and_requeues_a_done_page(q):
    put_page(q, "list", "CUR1")
    assert q.items("list")[0].key == "page:CUR1" and q.items("list")[0].payload == {"cursor": "CUR1"}
    assert q.claim("list").key == "page:CUR1"
    q.complete("list", "page:CUR1", {})
    put_page(q, "list", "CUR1")  # a relist walks pages that are already done
    assert q.count("list", "pending") == 1


def test_record_cursor_remembers_the_producing_page(q):
    record_cursor(q, "page:", "CUR1")
    record_cursor(q, "page:", "CUR1")  # the same page rerun after a crash: not a loop
    assert json.loads(q.get_meta("seen_cursors")) == {"CUR1": "page:"}


def test_record_cursor_raises_on_a_cursor_another_page_produced(q):
    record_cursor(q, "page:", "CUR1")
    with pytest.raises(PaginationLoop, match="cursor repeated: CUR1"):
        record_cursor(q, "page:CUR1", "CUR1")


def test_record_cursor_accepts_the_old_list_format(q):
    q.set_meta("seen_cursors", '["CUR0"]')  # producer unknown: any repeat of it is a loop
    record_cursor(q, "page:CUR1", "CUR2")
    with pytest.raises(PaginationLoop):
        record_cursor(q, "page:CUR2", "CUR0")


def test_pagination_loop_is_a_value_error():
    assert issubclass(PaginationLoop, ValueError)


def finish_listing(q):
    put_page(q, "list", None)
    q.complete("list", q.claim("list").key, {})
    record_cursor(q, "page:", "CUR1")
    put_page(q, "list", "CUR1")
    q.complete("list", q.claim("list").key, {})
    q.set_meta("list_done", "2026-11-26")


def test_restart_listing_walks_from_page_one_again(q):
    finish_listing(q)
    restart_listing(q, "list")
    assert q.get_meta("list_done") == "" and q.get_meta("seen_cursors") == "{}"
    assert q.item("list", "page:").status == "pending" and q.item("list", "page:CUR1").status == "done"
    record_cursor(q, "page:", "CUR1")  # the relist meets the same cursor again: not a loop


def test_restart_expired_cursor_keeps_list_done(q):
    finish_listing(q)
    q.set_meta("list_done", "")  # mid-listing
    assert restart_expired_cursor(q, "list", ValueError("bad cursor")) == {"restarted": True}
    assert q.get_meta("seen_cursors") == "{}" and q.item("list", "page:").status == "pending"


def test_first_claim_is_true_once_across_threads():
    first = FirstClaim()
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(first())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == [False] * 7 + [True] and first() is False
