import json

import pytest

from adrenalin.common.listing import PaginationLoop, page_key, put_page, record_cursor
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
