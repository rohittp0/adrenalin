import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.http import Cooldown, PacedSession
from adrenalin.common.listing import page_key
from adrenalin.common.ocr import OcrLine, OcrResult
from adrenalin.common.pipeline import Pipeline, WorkQueue
from adrenalin.tools.meta_ad_library.client import GRAPHQL_URL, SEARCH_DOC_ID, MetaClient, MetaQuery
from adrenalin.tools.meta_ad_library.hosts import HOSTS, MEDIA_SUFFIX
from adrenalin.tools.meta_ad_library.parse import parse_search_page
from adrenalin.tools.meta_ad_library.stages import (
    EXPIRED,
    FATAL,
    STAGE_NAMES,
    MetaContext,
    MetaStages,
    creative_from_dict,
    creative_to_dict,
    media_file,
    row_from_payload,
    row_items,
)

FIX = Path(__file__).parents[2] / "fixtures" / "meta_ad_library"
PAGE_ID = "120740541030"
DCO_ID = "1535930091652737"  # a DCO Creative with 2 Variations in search_page.json
TODAY = date(2026, 11, 26)
POLL = 0.005


def load(name: str) -> str:
    return (FIX / name).read_text()


def page(name: str, next_cursor: str | None = None) -> str:
    """A fixture page with its `page_info` set: the last page when `next_cursor` is None."""
    data = json.loads(load(name))
    info = data["data"]["ad_library_main"]["search_results_connection"]["page_info"]
    info["has_next_page"], info["end_cursor"] = next_cursor is not None, next_cursor
    return json.dumps(data)


def empty_page() -> str:
    data = json.loads(page("search_page.json"))
    data["data"]["ad_library_main"]["search_results_connection"]["edges"] = []
    return json.dumps(data)


def response(body: str | bytes = b"", status=200, url=GRAPHQL_URL) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.url = url
    return r


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeMeta:
    """GraphQL pages by cursor, the detail fixture, and fbcdn media (200 unless `media_status` says otherwise)."""

    def __init__(self, pages: dict[str | None, str]):
        self.pages = pages
        self.detail = load("detail_eu.json")
        self.media_status: dict[str, int] = {}  # full URL -> status (default 200)
        self.media: list[str] = []
        self.cursors: list[str | None] = []

    def __call__(self, method, url, data=None, headers=None, **kw):
        if url == GRAPHQL_URL:
            v = json.loads(data["variables"])
            if data["doc_id"] != SEARCH_DOC_ID:
                return response(self.detail)
            self.cursors.append(v["cursor"])
            return response(self.pages[v["cursor"]])
        assert urlparse(url).hostname.endswith(MEDIA_SUFFIX), url
        self.media.append(url)
        status = self.media_status.get(url, 200)
        return response(b"\xff\xd8JPEG" if status == 200 else b"", status, url=url)


def fake_recognize(path: Path) -> OcrResult:
    return OcrResult([OcrLine("Book now", 0.9, (0, 0, 1, 0.1)), OcrLine(" Flights ", 0.9, (0, 0.2, 1, 0.1))], "fake")


class Env:
    def __init__(self, tmp_path: Path, fake: FakeMeta, **flags):
        self.tmp, self.fake = tmp_path, fake
        self.sleeps: list[float] = []
        clock = Clock()
        self.session = PacedSession(Cooldown(tmp_path / "cooldown.sqlite3", clock=clock), HOSTS, transport=fake,
                                    sleep=self.sleeps.append, clock=clock)
        self.cache = ExtractionCache(tmp_path / "cache.sqlite3")
        self.entity, self.run_dir = tmp_path / "entity", tmp_path / "entity" / "runs" / "r"
        self.run_dir.mkdir(parents=True)
        self.ocr_calls: list[Path] = []

        def recognize(path):
            self.ocr_calls.append(path)
            return fake_recognize(path)

        self.ctx = MetaContext(
            client=MetaClient(self.session, today=lambda: TODAY), session=self.session,
            query=MetaQuery(PAGE_ID, "IN", "all", None, None), entity=self.entity, run=self.run_dir,
            cache=self.cache, today=lambda: TODAY, recognize=recognize, **flags)
        self.q = WorkQueue(self.run_dir / "state.sqlite3")

    def stages(self) -> MetaStages:
        return MetaStages(self.ctx)

    def run(self, only=None, st=None):
        st = st or self.stages()
        st.prepare(self.q, only)
        return Pipeline(self.q, st.stages(self.q, only), poll=POLL, fatal=FATAL).run()

    def reopen(self):
        self.q.close()
        self.q = WorkQueue(self.run_dir / "state.sqlite3")

    def close(self):
        self.q.close()
        self.cache.close()
        self.session.close()


@pytest.fixture
def env(tmp_path):
    made = []

    def make(pages, **flags):
        e = Env(tmp_path, FakeMeta(pages), **flags)
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


def dco():
    return next(c for c in parse_search_page(load("search_page.json")).creatives if c.library_id == DCO_ID)


# -- payloads -------------------------------------------------------------------


def test_creative_payload_round_trips_through_json():
    for c in parse_search_page(load("inactive_page.json")).creatives + [dco()]:
        assert creative_from_dict(json.loads(json.dumps(creative_to_dict(c)))) == c


def test_media_file_ignores_query_string():
    a = "https://scontent-a.xx.fbcdn.net/v/x/1.jpg?oh=1&oe=2"
    b = "https://scontent-b.xx.fbcdn.net/v/x/1.jpg?oh=3"
    name = hashlib.sha1(b"https://scontent-a.xx.fbcdn.net/v/x/1.jpg").hexdigest()[:20]
    assert media_file(a) == f"images/{name}.jpg"
    assert media_file(b) != media_file(a)


# -- stages and their config ----------------------------------------------------


def test_stage_selection(env):
    e = env({})
    names = lambda only: [s.name for s in e.stages().stages(e.q, only)]  # noqa: E731
    assert names(None) == ["list", "fetch"]
    assert names("list") == ["list"] and names("fetch") == ["fetch"] and names("export") == []
    e.ctx.eu_reach = e.ctx.ocr = True
    assert names(None) == list(STAGE_NAMES)
    assert names("list") == ["list", "detail"] and names("fetch") == ["fetch", "ocr"]
    by_name = {s.name: s for s in e.stages().stages(e.q)}
    assert (by_name["list"].concurrency, by_name["list"].throttle_on, by_name["list"].max_ahead) \
        == (1, ("detail", "fetch", "ocr"), 200)
    assert (by_name["fetch"].concurrency, by_name["fetch"].throttle_on) == (4, ("ocr",))


# -- list -----------------------------------------------------------------------


def test_listing_queues_one_row_per_creative(env):
    e = env({None: page("first_page.json", "C2"), "C2": page("search_page.json")})
    res = e.run()
    assert res.failed == {} and res.errors == []
    assert e.fake.cursors == [None, "C2"]
    assert e.q.get_meta("list_done") == TODAY.isoformat()
    items = {i.key: i for i in e.q.items("fetch")}
    assert len(items) == 40 and all(i.status == "done" for i in items.values())
    c, v, n = row_from_payload(items[DCO_ID].payload)
    assert (c.library_id, n, v) == (DCO_ID, 1, dco().variations[0])
    files = items[DCO_ID].result["image_files"]
    assert files and all((e.entity / f).read_bytes() == b"\xff\xd8JPEG" for f in files)
    assert files == [media_file(u) for u in dco().variations[0].image_urls]


def test_variations_queue_one_row_per_variation(env):
    e = env({None: page("search_page.json")}, variations=True)
    e.run()
    keys = [i.key for i in e.q.items("fetch")]
    assert f"{DCO_ID}:1" in keys and f"{DCO_ID}:2" in keys and DCO_ID not in keys
    _, v, n = row_from_payload(e.q.item("fetch", f"{DCO_ID}:2").payload)
    assert (n, v) == (2, dco().variations[1])
    assert len(keys) == 15


def test_expired_cursor_restarts_listing(env):
    e = env({None: page("search_page.json"), "OLD": json.dumps({"errors": [{"message": "bad cursor"}]})})
    e.q.put("list", page_key(None), {"cursor": ""})
    e.q.complete("list", page_key(None), {})
    e.q.put("list", page_key("OLD"), {"cursor": "OLD"})  # left by an earlier, interrupted run
    e.q.set_meta("seen_cursors", json.dumps({"OLD": "page:"}))
    st = e.stages()
    item = e.q.claim("list")
    assert item.key == "page:OLD"
    assert st.list_page(item, e.q) == {"restarted": True}
    assert e.q.item("list", "page:").status == "pending"
    assert e.q.get_meta("seen_cursors") == "{}"
    e.q.complete("list", item.key, {"restarted": True})
    res = e.run(st=st)
    assert res.failed == {} and e.q.get_meta("list_done") and len(e.q.items("fetch", "done")) == 10


def test_cursor_error_in_this_run_fails_the_page(env):
    e = env({None: page("search_page.json", "NEW"), "NEW": json.dumps({"errors": [{"message": "bad cursor"}]})})
    res = e.run()
    assert res.failed == {"list": 1} and e.q.item("list", "page:NEW").status == "failed"
    assert not e.q.get_meta("list_done")


def test_empty_page_completes(env):
    e = env({None: empty_page()})
    res = e.run()
    assert res.failed == {} and res.errors == []
    assert e.q.get_meta("list_done") == TODAY.isoformat()
    assert e.q.items("fetch") == []


def test_doc_not_found_stops_run(env):
    e = env({None: load("doc_not_found.json"), "OLD": load("doc_not_found.json")})
    res = e.run()
    assert len(res.errors) == 1 and res.errors[0].startswith("list: DocNotFound: ")
    assert res.failed == {} and e.q.count("list", "failed") == 0
    # a cursor carried over from an earlier run is not taken for an expired one
    e.q.put("list", page_key("OLD"), {"cursor": "OLD"})
    e.q.complete("list", page_key(None), {})
    res = e.run()
    assert res.errors and "DocNotFound" in res.errors[0] and e.q.item("list", "page:").status == "done"


# -- detail ---------------------------------------------------------------------


def test_eu_reach_goes_through_detail_to_fetch(env):
    e = env({None: page("search_page.json")}, eu_reach=True)
    res = e.run()
    assert res.failed == {}
    d = e.q.item("detail", DCO_ID)
    assert d.status == "done" and d.result["eu_reach"]["eu_total"] == 22
    f = e.q.item("fetch", DCO_ID)
    assert f.status == "done" and f.payload["eu_reach"] == d.result["eu_reach"]


# -- fetch ----------------------------------------------------------------------


def test_media_on_suffix_policy(env):
    e = env({})
    a = "https://scontent-maa5-1.xx.fbcdn.net/v/a.jpg?x=1"
    b = "https://scontent-maa3-2.xx.fbcdn.net/v/b.jpg?x=2"
    assert e.session.policy_key(urlparse(a).hostname) == e.session.policy_key(urlparse(b).hostname) == MEDIA_SUFFIX
    e.session.get(a)
    assert e.sleeps == []
    e.session.get(b)  # another host, same pace: the clock hasn't moved, so this request waits
    assert len(e.sleeps) == 1 and e.sleeps[0] > 0.5


def test_fetch_skips_existing_files_and_downloads_cards(env):
    carousel = next(c for c in parse_search_page(load("inactive_page.json")).creatives
                    if c.display_format == "CAROUSEL" and len(c.variations[0].cards) >= 2)
    e = env({None: page("inactive_page.json")})
    e.run()
    item = e.q.item("fetch", carousel.library_id)
    v = carousel.variations[0]
    expected = list(dict.fromkeys(media_file(u) for u in (*v.image_urls, *v.video_preview_urls,
                                                          *(c.image_url for c in v.cards if c.image_url))))
    assert item.result["image_files"] == expected and len(expected) >= 2
    before = len(e.fake.media)
    e.q.requeue("fetch", [carousel.library_id])
    e.run(only="fetch")
    assert len(e.fake.media) == before  # every file is already on disk


def test_expired_media_relists(env):
    old = page("search_page.json")
    e = env({None: old})
    old_urls = dco().variations[0].image_urls
    e.fake.media_status = {u: 403 for u in old_urls}
    e.run()
    failed = e.q.item("fetch", DCO_ID)
    assert failed.status == "failed" and "HTTP 403" in failed.error
    assert e.q.get_meta("list_done")
    tried = sum(u in old_urls for u in e.fake.media)

    e.reopen()  # the next run: the failed item is pending again, its URLs still expired
    fresh = old.replace("_nc_cat=", "_nc_cat=9")
    e.fake.pages[None] = fresh
    st = e.stages()
    st.prepare(e.q)
    assert e.q.item("list", "page:").status == "pending"
    assert not e.q.get_meta("list_done") and e.q.get_meta("seen_cursors") == "{}"
    assert e.q.item("fetch", DCO_ID) is None and e.q.item(EXPIRED, DCO_ID) is not None  # out of fetch's reach

    res = Pipeline(e.q, st.stages(e.q), poll=POLL, fatal=FATAL).run()
    assert res.failed == {}
    item = e.q.item("fetch", DCO_ID)
    assert item.status == "done"
    _, v, _ = row_from_payload(item.payload)
    assert v.image_urls and all("_nc_cat=9" in u for u in v.image_urls)  # refreshed with update_payload
    assert sum(u in old_urls for u in e.fake.media) == tried  # the expired URLs were never tried again
    assert e.q.items(EXPIRED) == [] and e.q.get_meta("list_done")
    e.fake.cursors.clear()
    e.stages().prepare(e.q)
    assert e.q.item("list", "page:").status == "done"  # nothing expired: no relist


def test_expired_media_relist_keeps_eu_reach(env):
    old = page("search_page.json")
    e = env({None: old}, eu_reach=True)
    e.fake.media_status = {u: 403 for u in dco().variations[0].image_urls}
    e.run()
    reach = e.q.item("detail", DCO_ID).result["eu_reach"]
    e.reopen()
    e.fake.pages[None] = old.replace("_nc_cat=", "_nc_cat=9")
    e.fake.detail = load("detail_no_eu.json")  # a second detail request would change the row's EU reach
    st = e.stages()
    st.prepare(e.q)
    assert e.q.item(EXPIRED, DCO_ID) is not None
    assert any(i.key == DCO_ID for i in row_items(e.q))  # still a row while the relist is unfinished
    assert Pipeline(e.q, st.stages(e.q), poll=POLL, fatal=FATAL).run().failed == {}
    item = e.q.item("fetch", DCO_ID)
    _, v, _ = row_from_payload(item.payload)
    assert item.status == "done" and all("_nc_cat=9" in u for u in v.image_urls)
    assert item.payload["eu_reach"] == reach and reach is not None
    assert e.q.items(EXPIRED) == []


def test_expired_media_of_delisted_creative_relists_once(env):
    e = env({None: page("search_page.json")})
    e.fake.media_status = {u: 410 for u in dco().variations[0].image_urls}
    e.run()
    e.reopen()
    data = json.loads(page("search_page.json"))
    conn = data["data"]["ad_library_main"]["search_results_connection"]
    conn["edges"] = [edge for edge in conn["edges"] if DCO_ID not in json.dumps(edge)]
    e.fake.pages[None] = json.dumps(data)
    e.run()  # relists; the Creative is gone, so its row goes back to fetch with its old URLs and fails again
    item = e.q.item("fetch", DCO_ID)
    assert item is not None and item.status == "failed" and "HTTP 410" in item.error
    assert e.q.items(EXPIRED) == [] and e.q.get_meta("list_done")
    e.reopen()
    e.stages().prepare(e.q)
    assert e.q.get_meta("list_done") and e.q.item("list", "page:").status == "done"  # not relisted again


def test_relist_without_list_stage_waits(env):
    e = env({None: page("search_page.json")})
    e.fake.media_status = {u: 403 for u in dco().variations[0].image_urls}
    e.run()
    e.reopen()
    e.stages().prepare(e.q, "fetch")  # `--stage fetch` can't relist
    assert e.q.get_meta("list_done") and e.q.item("fetch", DCO_ID) is not None


# -- ocr ------------------------------------------------------------------------


def test_ocr_reads_each_file_once_and_caches(env):
    e = env({None: page("search_page.json")}, ocr=True)
    assert e.run().failed == {}
    item = e.q.item("ocr", DCO_ID)
    files = e.q.item("fetch", DCO_ID).result["image_files"]
    assert item.status == "done"
    assert item.result["image_text"] == " || ".join(["Book now | Flights"] * len(files))
    stem = Path(files[0]).stem
    assert e.cache.get(f"fbimg:{stem}", "ocr")["lines"] == ["Book now", "Flights"]
    calls = len(e.ocr_calls)
    e.q.requeue("ocr", [DCO_ID])
    e.run(only="fetch")
    assert len(e.ocr_calls) == calls  # cached


def test_ocr_turned_on_later_reads_done_rows(env):
    e = env({None: page("search_page.json")})
    e.run()
    assert e.q.items("ocr") == []
    e.ctx.ocr = True
    e.run(only="fetch")
    assert e.q.item("ocr", DCO_ID).status == "done"
