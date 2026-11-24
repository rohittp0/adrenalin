import copy
import csv
import hashlib
import json
import socket
import subprocess
import threading
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
import requests

from adrenalin.common.http import Cooldown, PacedSession
from adrenalin.common.ocr import OcrLine, OcrResult, OcrUnavailable
from adrenalin.common.ocr.codex import CodexRunner
from adrenalin.common.output import EntityCollision
from adrenalin.common.pipeline import Item, WorkQueue
from adrenalin.tools.ads_transparency import sample_queue, sampling
from adrenalin.tools.ads_transparency.api import (
    COPY_STATUSES,
    CSV_FIELDS,
    ScrapeOptions,
    copy_status,
    infer_accounts,
    list_creatives,
    _progress_bars,
    scrape,
)
from adrenalin.tools.ads_transparency.rpc import Query, preview_ids

FIX = Path(__file__).parents[2] / "fixtures" / "ads_transparency"
AR = "AR04752318142645010433"
ENT = "ads-transparency/lascade-llc"
SEARCH = json.loads((FIX / "search_page.json").read_text())
DETAIL = json.loads((FIX / "creative_detail.json").read_text())
SIMGAD = (FIX / "simgad_text.png").read_bytes()
OEMBED = (FIX / "oembed.json").read_bytes()
CONTENT_TEXT = (FIX / "content_text.js").read_bytes()
CONTENT_VIDEO = (FIX / "content_video.js").read_bytes()
VIDEO_JS_URLS = {c["3"]["1"]["4"] for c in SEARCH["1"] if c.get("4") == 3 and "1" in c["3"]}
SIMGAD_CREATIVES = sum("simgad" in json.dumps(c["3"]) for c in SEARCH["1"])
POLL = 0.005


def response(status=200, body=b"{}", headers=None, url=""):
    r = requests.Response()
    r.status_code = status
    r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    r.url = url
    r.headers.update(headers or {})
    return r


def single_page():
    d = copy.deepcopy(SEARCH)
    del d["2"]
    return d


def two_pages():
    p1 = copy.deepcopy(SEARCH)
    p1["1"] = p1["1"][:20]
    p1["2"] = "CUR1"
    p2 = {"1": copy.deepcopy(SEARCH["1"][20:])}
    return p1, p2


class FakeGoogle:
    """Fake transport routed by URL. `search` maps the decoded f.req body to a dict or a Response."""

    def __init__(self, search=None):
        self.search = search or (lambda req: single_page())
        self.calls = Counter()
        self.search_bodies = []
        self.fail_simgad = 0
        self.oembed_status = 200
        self.oembed_reply = None
        self.hosts = []
        self.content_video = None
        self.missing_hosts = set()
        self.block_content_js = False
        self.content_override = None
        self.content_status = 200
        self.detail_reply = None  # a Response served for GetCreativeById instead of DETAIL
        self.lock = threading.Lock()

    def __call__(self, method, url, params=None, data=None, **kw):
        with self.lock:
            if "SearchCreatives" in url:
                self.calls["search"] += 1
                req = json.loads(data["f.req"])
                self.search_bodies.append(req)
                out = self.search(req)
                return out if isinstance(out, requests.Response) else response(body=out, url=url)
            if "GetCreativeById" in url:
                self.calls["detail"] += 1
                return self.detail_reply or response(body=DETAIL, url=url)
            if "simgad" in url:
                self.calls["simgad"] += 1
                if self.fail_simgad:
                    self.fail_simgad -= 1
                    return response(500, b"oops", url=url)
                return response(body=SIMGAD, headers={"Content-Type": "image/png"}, url=url)
            if "content.js" in url:
                self.calls["content_js"] += 1
                if self.block_content_js:
                    return response(302, b"", headers={"Location": "https://www.google.com/sorry/index"}, url=url)
                body = (self.content_video or CONTENT_VIDEO) if url in VIDEO_JS_URLS else CONTENT_TEXT
                body = self.content_override or body
                if self.content_status != 200:
                    return response(self.content_status, b"Not Found", url=url)
                return response(body=body, headers={"Content-Type": "text/javascript"}, url=url)
            if "oembed" in url:
                self.calls["oembed"] += 1
                if self.oembed_reply is not None:
                    return self.oembed_reply
                if self.oembed_status != 200:
                    return response(self.oembed_status, b"Unauthorized", url=url)
                return response(body=OEMBED, headers={"Content-Type": "application/json"}, url=url)
            if method == "GET" and url.endswith("/") and url.count("/") == 3:
                host = url.split("/")[2]  # domain check of an OCR-read host
                self.hosts.append(host)
                if host.removeprefix("www.") in self.missing_hosts:
                    raise requests.ConnectionError(f"Failed to resolve '{host}'") from socket.gaierror(8, "unknown")
                return response(404, b"", url=url)
        raise AssertionError(f"unexpected request {method} {url}")


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_session(tmp_path, fake, clock=None, sleep=None, wait_on_block=False):
    clock = clock or Clock()
    return PacedSession(Cooldown(tmp_path / "test-cooldown.sqlite3", clock=clock), intervals={},
                        transport=fake, sleep=sleep or (lambda s: None), clock=clock, wait_on_block=wait_on_block)


BROKEN_IDS: set[str] = set()  # image names (file stems) that OCR reads as Google's error page; "*" = all
BROKEN_OCR_LINES = ["500.", "That’s an error.", "There was an error. Please try again later."]


@pytest.fixture(autouse=True)
def _reset_broken_ids():
    BROKEN_IDS.clear()


def fake_ocr(path):
    if BROKEN_IDS & {"*", Path(path).stem}:
        return OcrResult([OcrLine(t, 0.95, (0.1, 0.1 + i * 0.1, 0.5, 0.05))
                          for i, t in enumerate(BROKEN_OCR_LINES)], "apple_vision")
    lines = [
        OcrLine("Lascade", 0.95, (0.05, 0.05, 0.3, 0.05)),
        OcrLine("www.lascade.com", 0.95, (0.05, 0.12, 0.4, 0.05)),
        OcrLine("Big Bold Headline", 0.95, (0.05, 0.2, 0.8, 0.1)),
        OcrLine("A smaller description line.", 0.95, (0.05, 0.35, 0.45, 0.05)),
    ]
    return OcrResult(lines, "apple_vision")


class FakeCodexRun:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        with self.lock:
            self.calls += 1
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({
            "brand": "Lascade", "display_url": "lascade.com", "headline": "Vision headline",
            "description": "Vision description", "cta": "", "full_text": "Vision", "confidence": 0.9}))
        return subprocess.CompletedProcess(argv, 0, "", "")


def codex(limit=200, run=None):
    return CodexRunner("fake-model", limit=limit, run=run or FakeCodexRun())


def run_scrape(tmp_path, fake, session=None, codex_runner=None, **opts):
    opts.setdefault("sampling_mode", "novelty")  # Existing tests exercise the legacy discovery mode.
    return scrape(ScrapeOptions(AR, out=tmp_path, **opts), session=session or make_session(tmp_path, fake),
                  ocr=fake_ocr, codex=codex_runner or codex(), log=lambda m: None, poll=POLL)


def read_rows(res):
    return list(csv.DictReader(res.csv_path.open(encoding="utf-8")))


def run_dir(tmp_path, name="anywhere_all_all"):
    return tmp_path / ENT / "runs" / name


def test_scrape_writes_one_row_per_creative(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake)
    rows = read_rows(res)
    assert res.csv_path == tmp_path / "ads-transparency/lascade-llc/runs/anywhere_all_all/ads.csv"
    assert len(rows) == 40 == res.rows and tuple(rows[0]) == CSV_FIELDS
    assert len({r["creative_id"] for r in rows}) == 40
    assert {r["variation"] for r in rows} == {"1"}
    assert all(r["transparency_url"].endswith(f"/creative/{r['creative_id']}?region=anywhere") for r in rows)
    assert all(r["transparency_url"].startswith(f"https://adstransparency.google.com/advertiser/{AR}/")
               for r in rows)
    v = next(r for r in rows if r["format"] == "video")
    assert v["video_title"] == "Rotating Tiles video" and v["extraction_method"] == "content_js"
    assert v["video_id"] == "KVHq8nrh0GI" and v["video_url"] == "https://www.youtube.com/watch?v=KVHq8nrh0GI"
    i = next(r for r in rows if r["image_file"])
    assert (tmp_path / ENT / i["image_file"]).exists()
    assert i["extraction_method"] == "apple_vision" and i["headline"] == "Big Bold Headline"
    t = next(r for r in rows if r["format"] == "text" and not r["image_file"])
    assert t["brand"] == "Hotels70" and " | " in t["sitelinks"] and t["extraction_confidence"] == "1.0"
    assert all(r["first_shown"] and r["last_shown"] and r["days_shown"] for r in rows)
    assert res.blocked_until is None and res.failed == {} and res.vision_skipped == 0
    assert json.loads((tmp_path / ".cache/advertisers.json").read_text()) == {AR: "Lascade LLC"}
    assert not list((tmp_path / ENT / "images").glob("*.part"))


def test_every_row_is_one_line_with_full_text_lines_joined(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle())
    rows = read_rows(res)
    # viewers that split rows on line breaks (ignoring CSV quoting) must still see one row per line
    assert len(res.csv_path.read_text(encoding="utf-8").splitlines()) == len(rows) + 1
    i = next(r for r in rows if r["extraction_method"] == "apple_vision")
    assert i["full_text"] == "Lascade | www.lascade.com | Big Bold Headline | A smaller description line."


def test_scrape_variations_mode_one_row_per_variation(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, variations=True)
    rows = read_rows(res)
    assert res.csv_path.parent.name.endswith("_variations")
    assert len(rows) == 80 == res.rows
    assert {r["variation"] for r in rows} == {"1", "2"}
    assert fake.calls["detail"] == 40
    assert len(list((tmp_path / ENT / "images").iterdir())) == 2


def test_variations_relist_refreshes_listing_fields(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, variations=True)
    bumped = single_page()
    for c in bumped["1"]:
        c["13"] = c["13"] + 1
    fake.search = lambda req: bumped
    res = run_scrape(tmp_path, fake, variations=True)
    want = {c["2"]: str(c["13"]) for c in bumped["1"]}
    rows = read_rows(res)
    assert len(rows) == 80 and all(r["days_shown"] == want[r["creative_id"]] for r in rows)
    assert fake.calls["detail"] == 40


def test_rerun_with_closed_window_fetches_nothing_new(tmp_path):
    fake = FakeGoogle()
    opts = dict(start=date(2026, 1, 1), end=date(2026, 6, 30))
    first = read_rows(run_scrape(tmp_path, fake, **opts))
    before = Counter(fake.calls)
    res = run_scrape(tmp_path, fake, **opts)
    assert fake.calls == before
    assert res.csv_path.parent.name == "anywhere_2026-01-01_2026-06-30"
    assert read_rows(res) == first and res.rows == 40


def test_rerun_with_open_window_relists_and_refreshes(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    before = Counter(fake.calls)
    bumped = single_page()
    for c in bumped["1"]:
        c["13"] = c["13"] + 1
    fake.search = lambda req: bumped
    res = run_scrape(tmp_path, fake)
    assert fake.calls["search"] > before["search"]
    assert fake.calls["simgad"] == before["simgad"] and fake.calls["content_js"] == before["content_js"]
    want = {c["2"]: str(c["13"]) for c in bumped["1"]}
    rows = read_rows(res)
    assert len(rows) == 40 and all(r["days_shown"] == want[r["creative_id"]] for r in rows)


def test_open_window_relist_walks_every_page(tmp_path):
    p1, p2 = two_pages()
    fake = FakeGoogle(search=lambda req: p2 if req.get("4") == "CUR1" else p1)
    run_scrape(tmp_path, fake)
    cursor_calls = sum(1 for b in fake.search_bodies if b.get("4") == "CUR1")
    res = run_scrape(tmp_path, fake)
    assert sum(1 for b in fake.search_bodies if b.get("4") == "CUR1") == cursor_calls + 1
    assert res.rows == 40
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        assert q.get_meta("list_done") == date.today().isoformat()
    finally:
        q.close()


def test_ocr_domains_checked_once_per_host(tmp_path):
    fake = FakeGoogle()
    rows = read_rows(run_scrape(tmp_path, fake))
    texts = [r for r in rows if r["image_file"] and r["format"] == "text"]
    assert texts and {(r["website"], r["extraction_method"]) for r in texts} == {("lascade.com", "apple_vision")}
    assert fake.hosts == ["www.lascade.com"]  # the host as shown; content.js rows are never checked
    read_rows(run_scrape(tmp_path, fake))
    assert fake.hosts == ["www.lascade.com"]  # an existing host is remembered across runs


def test_unresolvable_ocr_domain_sends_row_to_codex(tmp_path):
    fake = FakeGoogle()
    fake.missing_hosts = {"lascade.com"}
    logs = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, fake), ocr=fake_ocr,
                 codex=codex(), log=logs.append, poll=POLL)
    texts = [r for r in read_rows(res) if r["image_file"] and r["format"] == "text"]
    assert texts and {r["extraction_method"] for r in texts} == {"codex"}
    assert sorted(fake.hosts) == ["lascade.com", "www.lascade.com"]  # both forms tried, once each
    assert sum("lascade.com" in m and "resolve" in m for m in logs) == 1


def test_rerun_with_vision_requeues_ocr_rows(tmp_path):
    fake = FakeGoogle()
    first = run_scrape(tmp_path, fake)
    assert {r["extraction_method"] for r in read_rows(first) if r["image_file"]} == {"apple_vision"}
    run = FakeCodexRun()
    res = run_scrape(tmp_path, fake, codex_runner=codex(run=run), vision=True)
    rows = read_rows(res)
    images = [r for r in rows if r["image_file"]]
    assert len(images) == SIMGAD_CREATIVES
    assert {r["extraction_method"] for r in images} == {"codex"}
    assert {r["extraction_model"] for r in images} == {"fake-model"}
    assert all(r["headline"] == "Vision headline" for r in images)
    assert {r["extraction_method"] for r in rows if not r["image_file"]} == {"content_js"}
    assert run.calls == SIMGAD_CREATIVES


def test_codex_limit_rows_requeued_next_run(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, codex_runner=codex(limit=1), vision=True)
    images = [r for r in read_rows(res) if r["image_file"]]
    assert sum(r["extraction_method"] == "codex" for r in images) == 1
    assert res.vision_skipped == SIMGAD_CREATIVES - 1
    assert "vision_pending" not in CSV_FIELDS
    res2 = run_scrape(tmp_path, fake, codex_runner=codex(limit=5), vision=True)
    images = [r for r in read_rows(res2) if r["image_file"]]
    assert sum(r["extraction_method"] == "codex" for r in images) == 6
    assert res2.vision_skipped == SIMGAD_CREATIVES - 6


BAD_REQUEST = response(400, b"<html><body>Bad Request</body></html>", {"Content-Type": "text/html"})
SORRY = response(302, b"", {"Location": "https://www.google.com/sorry/index"})


def cursor_calls(fake):
    return sum(1 for b in fake.search_bodies if b.get("4") == "CUR1")


def test_expired_cursor_restarts_listing(tmp_path):
    # A cursor resumed from an earlier run is rejected: listing restarts from page 1.
    p1, p2 = two_pages()
    replies = [SORRY, BAD_REQUEST]
    fake = FakeGoogle(search=lambda req: (replies.pop(0) if replies else p2) if req.get("4") == "CUR1" else p1)
    clock = Clock()
    session = make_session(tmp_path, fake, clock)
    assert run_scrape(tmp_path, fake, session=session).blocked_until is not None
    clock.now += 1000
    res = run_scrape(tmp_path, fake, session=session)
    rows = read_rows(res)
    assert len(rows) == 40 == len({r["creative_id"] for r in rows})
    assert cursor_calls(fake) == 3  # sorry, rejected resume, after the restart
    assert res.failed == {} and res.blocked_until is None
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        assert q.get_meta("list_done") == date.today().isoformat()
    finally:
        q.close()


def test_cursor_error_mid_run_fails_item_and_retries_next_run(tmp_path):
    # A cursor obtained in this run is not "expired": its error fails the item (no restart).
    p1, p2 = two_pages()
    replies = [BAD_REQUEST]
    fake = FakeGoogle(search=lambda req: (replies.pop(0) if replies else p2) if req.get("4") == "CUR1" else p1)
    res = run_scrape(tmp_path, fake)
    assert res.failed == {"list": 1} and res.rows == 20
    page1_calls = sum(1 for b in fake.search_bodies if "4" not in b)
    res2 = run_scrape(tmp_path, fake)
    assert res2.failed == {} and res2.rows == 40
    assert sum(1 for b in fake.search_bodies if "4" not in b) == page1_calls  # no restart from page 1


def test_non_200_search_fails_list_instead_of_truncating(tmp_path):
    p1, p2 = two_pages()
    replies = [response(500, {})]
    fake = FakeGoogle(search=lambda req: (replies.pop(0) if replies else p2) if req.get("4") == "CUR1" else p1)
    res = run_scrape(tmp_path, fake)
    assert res.failed == {"list": 1} and res.rows == 20
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        assert not q.get_meta("list_done")
    finally:
        q.close()
    assert run_scrape(tmp_path, fake).rows == 40


def test_cursor_page_with_no_creatives_warns(tmp_path):
    p1, _ = two_pages()
    fake = FakeGoogle(search=lambda req: {"1": []} if req.get("4") == "CUR1" else p1)
    logs = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, fake), ocr=fake_ocr,
                 codex=codex(), log=logs.append, poll=POLL)
    assert res.rows == 20 and res.failed == {}
    assert any("page:CUR1" in m and "no creatives" in m for m in logs)


def test_list_page_rerun_after_crash_is_not_a_pagination_loop(tmp_path):
    # A crash after the page recorded its next cursor but before it was marked done reruns the page.
    p1, p2 = two_pages()
    fake = FakeGoogle(search=lambda req: p2 if req.get("4") == "CUR1" else p1)
    opts = dict(start=date(2026, 1, 1), end=date(2026, 6, 30))  # closed window: no relist
    run_scrape(tmp_path, fake, **opts)
    q = WorkQueue(run_dir(tmp_path, "anywhere_2026-01-01_2026-06-30") / "state.sqlite3")
    try:
        q.requeue("list", ["page:"])
    finally:
        q.close()
    res = run_scrape(tmp_path, fake, **opts)
    assert res.failed == {} and res.rows == 40


def test_old_seen_cursors_list_is_accepted(tmp_path):
    p1, p2 = two_pages()
    fake = FakeGoogle(search=lambda req: p2 if req.get("4") == "CUR1" else p1)
    opts = dict(start=date(2026, 1, 1), end=date(2026, 6, 30))
    run_scrape(tmp_path, fake, stage="list", **opts)
    q = WorkQueue(run_dir(tmp_path, "anywhere_2026-01-01_2026-06-30") / "state.sqlite3")
    try:
        q.set_meta("seen_cursors", '["CUR0"]')
        q.requeue("list", ["page:CUR1"])
    finally:
        q.close()
    assert run_scrape(tmp_path, fake, **opts).failed == {}


def test_transient_oembed_failure_is_retried_next_run(tmp_path):
    fake = FakeGoogle()
    fake.oembed_status = 503
    first = read_rows(run_scrape(tmp_path, fake))
    videos = [r for r in first if r["video_id"]]
    assert videos and all(r["video_title"] == "" and r["brand"] for r in videos)
    js_calls, oembed_calls = fake.calls["content_js"], fake.calls["oembed"]
    fake.oembed_status = 200
    rows = read_rows(run_scrape(tmp_path, fake))
    videos = [r for r in rows if r["video_id"]]
    assert videos and all(r["video_title"] == "Rotating Tiles video" for r in videos)
    # only oEmbed is retried: the rest of the row is kept and content.js is not fetched again
    assert fake.calls["content_js"] == js_calls and fake.calls["oembed"] == oembed_calls + len(videos)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "video_title"} for r in rs]
    assert strip(rows) == strip(first)
    assert "title_pending" not in CSV_FIELDS
    rows3 = read_rows(run_scrape(tmp_path, fake))
    assert rows3 == rows and fake.calls["oembed"] == oembed_calls + len(videos)  # done: not asked again


def test_title_retry_stops_at_first_failure(tmp_path):
    fake = FakeGoogle()
    fake.oembed_status = 503
    run_scrape(tmp_path, fake)
    before = fake.calls["oembed"]
    run_scrape(tmp_path, fake)
    assert fake.calls["oembed"] == before + 1  # YouTube still failing: one try, the rest wait


def test_codex_limit_warns_once_per_run(tmp_path):
    fake = FakeGoogle()
    logs = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, vision=True), session=make_session(tmp_path, fake),
                 ocr=fake_ocr, codex=codex(limit=1), log=logs.append, poll=POLL)
    assert res.vision_skipped == SIMGAD_CREATIVES - 1
    limit_lines = [m for m in logs if "vision limit" in m]
    assert len(limit_lines) == 1 and str(SIMGAD_CREATIVES - 1) in limit_lines[0]


def test_permanent_oembed_failure_is_not_retried(tmp_path):
    fake = FakeGoogle()
    fake.oembed_status = 401  # embedding disabled / private video
    run_scrape(tmp_path, fake)
    before = Counter(fake.calls)
    run_scrape(tmp_path, fake)
    assert fake.calls["oembed"] == before["oembed"] and fake.calls["content_js"] == before["content_js"]


def test_content_js_without_copy_is_no_copy(tmp_path):
    fake = FakeGoogle()
    fake.content_video = (b"var a = {'adData': [{'squareImage': "
                          b"'https://tpc.googlesyndication.com/simgad/12147114744176439645'}]};")
    logs = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, fake), ocr=fake_ocr,
                 codex=codex(), log=logs.append, poll=POLL)
    videos = [r for r in read_rows(res) if r["format"] == "video"]
    assert videos and all(r["image_urls"].endswith("/12147114744176439645") for r in videos)
    assert all(r["extraction_confidence"] == "" and r["brand"] == "" for r in videos)
    assert not any("no ad copy" in m for m in logs)
    assert all(r["copy_status"] == "no_copy" for r in videos)


def test_oembed_failure_blanks_title_without_failing(tmp_path):
    fake = FakeGoogle()
    fake.oembed_status = 401
    res = run_scrape(tmp_path, fake)
    v = next(r for r in read_rows(res) if r["format"] == "video")
    assert v["video_title"] == "" and v["video_id"] == "KVHq8nrh0GI"
    assert res.failed == {}


def test_failed_items_exit_1_and_retry_next_run(tmp_path):
    fake = FakeGoogle()
    fake.fail_simgad = 1
    res = run_scrape(tmp_path, fake)
    assert res.failed == {"fetch": 1}
    assert res.rows == 40  # the failed creative still has its listing row
    res2 = run_scrape(tmp_path, fake)
    rows = read_rows(res2)
    assert len(rows) == 40 and res2.failed == {}
    assert all(r["extraction_method"] for r in rows)


def test_block_mid_list_exports_partial_and_reports(tmp_path):
    p1, p2 = two_pages()
    sorry = response(302, b"", {"Location": "https://www.google.com/sorry/index?continue=x"})
    state = {"blocked": True}
    fake = FakeGoogle(search=lambda req: (sorry if state["blocked"] else p2) if req.get("4") == "CUR1" else p1)
    clock = Clock()
    session = make_session(tmp_path, fake, clock)
    res = run_scrape(tmp_path, fake, session=session)
    assert res.blocked_until == clock.now + 900
    rows = read_rows(res)
    assert {r["creative_id"] for r in rows} == {c["2"] for c in p1["1"]}
    assert all(r["extraction_method"] for r in rows)
    state["blocked"] = False
    # A restart during the cooldown must leave the saved page pending without sending another request.
    session = make_session(tmp_path, fake, clock)
    before = len(fake.search_bodies)
    assert run_scrape(tmp_path, fake, session=session).blocked_until is not None
    assert len(fake.search_bodies) == before
    clock.now += 1000
    res2 = run_scrape(tmp_path, fake, session=session)
    assert [body.get("4") for body in fake.search_bodies[before:]] == ["CUR1"]
    assert res2.blocked_until is None and res2.rows == 40 and res2.failed == {}


def test_summary_written_with_status_counts_and_accounts(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle())
    summary = json.loads((res.csv_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert res.summary_path == res.csv_path.parent / "summary.json"
    assert res.listing_complete is True and summary["listing_complete"] is True
    assert summary["listed"] == res.rows == res.listed
    assert sum(summary["copy_status"].values()) == res.rows
    assert set(summary["copy_status"]) <= set(COPY_STATUSES)
    assert summary["coverage"] is None and summary["sampling"] is None and summary["groups"] == []
    assert {"busy", "waiting"} <= set(next(iter(summary["stage_seconds"].values())))
    for a in summary["ads_accounts"]:
        assert a["ads_account_id"] and a["source"] in ("exposed", "inferred")
    counts = [sum(a["websites"].values()) for a in summary["ads_accounts"]]
    assert counts == sorted(counts, reverse=True)


def test_block_mid_list_marks_incomplete(tmp_path):
    p1, p2 = two_pages()
    sorry = response(302, b"", {"Location": "https://www.google.com/sorry/index?continue=x"})
    fake = FakeGoogle(search=lambda req: sorry if req.get("4") == "CUR1" else p1)
    lines = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, fake), ocr=fake_ocr,
                 codex=codex(), log=lines.append, poll=POLL)
    assert res.listing_complete is False and res.listed == 20
    summary = json.loads(res.summary_path.read_text(encoding="utf-8"))
    assert summary["listing_complete"] is False and summary["listed"] == 20
    assert lines[-1].startswith("INCOMPLETE: listed 20 of ~")
    assert lines[-1].endswith("Creatives; rerun the same command to continue")


def test_zero_creatives_in_window_writes_header_only(tmp_path):
    fake = FakeGoogle(search=lambda req: {"1": []} if {"6", "7"} & set(req["3"]) else single_page())
    res = run_scrape(tmp_path, fake, start=date(2026, 11, 1), end=date(2026, 11, 2))
    assert res.rows == 0
    assert res.csv_path.read_text(encoding="utf-8").splitlines() == [",".join(CSV_FIELDS)]
    assert res.failed == {} and res.blocked_until is None


def test_unknown_advertiser_raises(tmp_path):
    fake = FakeGoogle(search=lambda req: {"1": []})
    with pytest.raises(ValueError, match="No creatives found for AR"):
        run_scrape(tmp_path, fake)


def test_stage_extract_only_does_not_call_rpc(tmp_path):
    p1, p2 = two_pages()
    sorry = response(302, b"", {"Location": "https://www.google.com/sorry/index"})
    fake = FakeGoogle(search=lambda req: sorry if req.get("4") == "CUR1" else p1)
    session = make_session(tmp_path, fake)
    assert run_scrape(tmp_path, fake, session=session).blocked_until is not None
    searches = fake.calls["search"]
    res = run_scrape(tmp_path, fake, session=session, stage="extract")
    assert fake.calls["search"] == searches
    assert res.rows == 20 and res.blocked_until is None


def test_stage_list_only_queues_without_fetching(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, stage="list")
    assert fake.calls["simgad"] == fake.calls["content_js"] == 0
    rows = read_rows(res)
    assert len(rows) == 40 and not any(r["extraction_method"] for r in rows)
    res = run_scrape(tmp_path, fake, stage="export")
    assert res.rows == 40 and fake.calls["simgad"] == 0


def test_existing_folder_for_other_advertiser_raises(tmp_path):
    d = tmp_path / ENT
    d.mkdir(parents=True)
    (d / "entity.json").write_text(json.dumps({"id": "AR999", "name": "Lascade LLC"}))
    with pytest.raises(EntityCollision):
        run_scrape(tmp_path, FakeGoogle())


def test_list_creatives_yields_every_page(tmp_path):
    p1, p2 = two_pages()
    fake = FakeGoogle(search=lambda req: p2 if req.get("4") == "CUR1" else p1)
    got = list(list_creatives(AR, session=make_session(tmp_path, fake)))
    assert len(got) == 40 and got[0].advertiser_name == "Lascade LLC"


def test_stage_export_after_codex_limit_keeps_copy_and_pending(tmp_path):
    fake = FakeGoogle()
    first = read_rows(run_scrape(tmp_path, fake, codex_runner=codex(limit=1), vision=True))
    run = FakeCodexRun()
    assert read_rows(run_scrape(tmp_path, fake, codex_runner=codex(run=run), vision=True, stage="export")) == first
    assert run.calls == 0
    rows = read_rows(run_scrape(tmp_path, fake, codex_runner=codex(limit=5), vision=True))
    assert sum(r["extraction_method"] == "codex" for r in rows if r["image_file"]) == 6


def test_stage_export_with_vision_does_not_record_vision(tmp_path):
    fake = FakeGoogle()
    first = read_rows(run_scrape(tmp_path, fake))
    assert read_rows(run_scrape(tmp_path, fake, vision=True, stage="export")) == first
    rows = read_rows(run_scrape(tmp_path, fake, vision=True))
    assert {r["extraction_method"] for r in rows if r["image_file"]} == {"codex"}


def test_content_js_kept_out_of_queue_and_deleted_after_extract(tmp_path):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake)
    assert res.rows == 40 and res.failed == {}
    assert not list((run_dir(tmp_path) / "content").glob("*"))
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        payloads = [i.payload for i in q.items("extract")]
        assert payloads and all("js" not in p and len(json.dumps(p)) < 4000 for p in payloads)
        assert sum("content_file" in p for p in payloads) == 5
    finally:
        q.close()


def test_leftover_content_files_of_done_items_are_removed_on_start(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        rel = next(i.payload["content_file"] for i in q.items("extract") if "content_file" in i.payload)
    finally:
        q.close()
    leftover = run_dir(tmp_path) / rel
    leftover.write_text("stale")
    run_scrape(tmp_path, fake, stage="export")
    assert not leftover.exists()


def test_missing_content_file_refetches(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, stage="list")
    run_scrape(tmp_path, fake, stage="fetch")
    files = list((run_dir(tmp_path) / "content").glob("*.js"))
    assert len(files) == 5
    files[0].unlink()
    before = fake.calls["content_js"]
    res = run_scrape(tmp_path, fake)
    rows = read_rows(res)
    assert res.failed == {} and fake.calls["content_js"] == before + 1
    assert sum(r["extraction_method"] == "content_js" for r in rows) == 5
    assert not list((run_dir(tmp_path) / "content").glob("*"))


def test_second_vision_run_retries_failed_codex(tmp_path):
    fake = FakeGoogle()

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "codex down")

    first = read_rows(run_scrape(tmp_path, fake, codex_runner=codex(run=failing), vision=True))
    assert {r["extraction_method"] for r in first if r["image_file"]} == {"apple_vision"}
    rows = read_rows(run_scrape(tmp_path, fake, vision=True))
    assert {r["extraction_method"] for r in rows if r["image_file"]} == {"codex"}


def no_ocr(path):
    raise OcrUnavailable("No OCR engine: install tesseract")


@pytest.mark.parametrize("codex_run", ["exit-1", "missing"])
def test_no_ocr_engine_and_no_codex_fails_image_items(tmp_path, codex_run):
    fake, calls = FakeGoogle(), []

    def run(argv, **kwargs):
        calls.append(argv)
        if codex_run == "missing":
            raise FileNotFoundError("codex")
        return subprocess.CompletedProcess(argv, 1, "", "codex down")

    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, fake), ocr=no_ocr,
                 codex=codex(run=run), log=lambda m: None, poll=POLL)
    assert res.failed == {"extract": SIMGAD_CREATIVES} and res.rows == 40
    rows = read_rows(res)
    assert not any(r["extraction_method"] for r in rows if r["image_file"])  # no blank "done" rows
    if codex_run == "missing":
        assert 1 <= len(calls) <= 2  # circuit breaker: only calls already in flight (2 extract workers)
    else:
        assert len(calls) == SIMGAD_CREATIVES


def test_cli_exits_1_when_no_ocr_engine_and_no_codex(tmp_path, monkeypatch, capsys):
    from adrenalin.cli import main
    from adrenalin.tools.ads_transparency import api

    fake = FakeGoogle()
    real = api.scrape

    def scrape_offline(options, **kw):
        def missing(argv, **kwargs):
            raise FileNotFoundError("codex")
        return real(options, session=make_session(tmp_path, fake), ocr=no_ocr, codex=codex(run=missing),
                    log=lambda m: None, poll=POLL)

    monkeypatch.setattr(api, "scrape", scrape_offline)
    assert main(["ads-transparency", AR, "--out", str(tmp_path)]) == 1
    assert f"extract: {SIMGAD_CREATIVES}" in capsys.readouterr().err


@pytest.mark.parametrize("reply", [SORRY, response(429, b"slow down")], ids=["sorry", "429"])
def test_oembed_block_never_waits_under_wait_on_block(tmp_path, reply):
    fake = FakeGoogle()
    fake.oembed_reply = reply
    clock, sleeps = Clock(), []

    def sleep(s):
        sleeps.append(s)
        clock.now += s

    session = make_session(tmp_path, fake, clock, sleep=sleep, wait_on_block=True)
    res = run_scrape(tmp_path, fake, session=session)
    videos = [r for r in read_rows(res) if r["video_id"]]
    assert videos and all(r["video_title"] == "" for r in videos)
    assert res.failed == {} and res.rows == 40
    assert max(sleeps, default=0) <= 0.5  # pacing only: no 900 s cooldown wait, no 60 s backoff
    assert fake.calls["oembed"] <= 2


def test_blocked_script_host_stops_listing_but_images_still_finish(tmp_path):
    p1, p2 = two_pages()
    fake = FakeGoogle(search=lambda req: p2 if req.get("4") == "CUR1" else p1)
    fake.block_content_js = True
    clock = Clock()
    res = run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock))
    assert res.blocked_until is not None
    assert fake.calls["content_js"] == 1  # nothing more is sent to a host in cooldown
    rows = read_rows(res)
    images = [r for r in rows if r["image_file"]]
    assert images and all(r["extraction_method"] for r in images)  # fetch kept going
    scripts = [r for r in rows if "content.js" in r["preview_url"]]
    assert scripts and not any(r["extraction_method"] for r in scripts)

    # after the cooldown a rerun picks up the script downloads and the rest of the listing
    fake.block_content_js = False
    clock.now += 901
    res = run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock))
    assert res.blocked_until is None and not res.failed
    rows = read_rows(res)
    assert len(rows) == len(SEARCH["1"])
    assert all(r["extraction_method"] for r in rows if "content.js" in r["preview_url"] and r["format"] != "video")


def test_unparsed_content_js_is_kept_for_inspection(tmp_path):
    fake = FakeGoogle()
    fake.content_override = b"(function(){var nothing=1;})();"
    warnings = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, fake), ocr=fake_ocr,
                 codex=codex(), log=warnings.append, poll=POLL)
    kept = list((res.csv_path.parent / "content").glob("*.js"))
    unparsed = [w for w in warnings if "not parsed" in w]
    # scripts whose copy came from the URL's overlay= parameter are parsed and deleted as usual
    assert kept and len(kept) == len(unparsed) < fake.calls["content_js"]
    assert all("kept at" in w for w in unparsed)
    # a rerun's startup cleanup leaves them too
    run_scrape(tmp_path, fake, stage="export")
    assert len(list((res.csv_path.parent / "content").glob("*.js"))) == len(kept)


def test_progress_bars_show_creatives_and_stages_and_carry_the_log(tmp_path, capsys):
    fake = FakeGoogle()
    fake.oembed_status = 401  # makes the run log a warning while the bars show
    messages = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, progress=True), session=make_session(tmp_path, fake),
                 ocr=fake_ocr, codex=codex(), log=messages.append, poll=POLL)
    err = capsys.readouterr().err
    n = len(SEARCH["1"])
    assert res.rows == n
    for label in ("creatives", "list", "fetch", "scripts", "extract"):
        assert label in err
    assert f"{n}/{n}" in err  # the listing finished, so the creatives total is exact
    assert any("oEmbed failed" in m for m in err.splitlines()) and not any("oEmbed" in m for m in messages)
    assert any(m.startswith("exported") for m in messages)  # after the bars close, back to the caller's log
    q = WorkQueue(res.csv_path.parent / "state.sqlite3")
    assert q.get_meta("count_estimate") == "600"  # Google's band on the first page
    q.close()


# -- Broken Preview ---------------------------------------------------------------------------------

BROKEN_ID = "877210798417700731"  # a listed image; DETAIL's other Variation is 10849098364905980222


def extract_results(tmp_path, run="anywhere_all_all"):
    q = WorkQueue(run_dir(tmp_path, run) / "state.sqlite3")
    try:
        return {k: r or {} for k, r in q.results("extract")}
    finally:
        q.close()


def broken_key(tmp_path):
    return next(k for k, r in extract_results(tmp_path).items() if r.get("preview_broken"))


def broken_creative():
    """Creative ID of the listing whose image is BROKEN_ID."""
    from adrenalin.tools.ads_transparency.rpc import parse_search_page
    return next(c.creative_id for c in parse_search_page(SEARCH).creatives
                if c.preview and c.preview.image_id == BROKEN_ID)


def row_of(res, creative_id, variation="1"):
    return next(r for r in read_rows(res) if r["creative_id"] == creative_id and r["variation"] == variation)


def test_broken_preview_recovered_from_other_variation(tmp_path):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake)
    assert res.failed == {} and fake.calls["detail"] == 1
    results = extract_results(tmp_path)
    got = next(r for k, r in results.items() if r.get("variation_used"))
    assert got["variation_used"] == 2 and not got.get("preview_broken") and got["headline"] == "Big Bold Headline"
    assert not any(r.get("preview_broken") for r in results.values())
    assert (tmp_path / ENT / "images" / "10849098364905980222.png").exists()
    row = row_of(res, broken_creative())
    assert row["copy_status"] == "read" and row["headline"] == "Big Bold Headline"
    # the row shows the Variation that was read, not the broken one
    assert row["image_file"] == "images/10849098364905980222.png"
    assert row["preview_url"].endswith("/10849098364905980222")


def test_broken_preview_all_variations_broken(tmp_path):
    BROKEN_IDS.add("*")
    fake = FakeGoogle()
    codex_run = FakeCodexRun()
    res = run_scrape(tmp_path, fake, codex_runner=codex(run=codex_run))
    assert res.failed == {} and codex_run.calls == 0
    broken = [r for r in extract_results(tmp_path).values() if r.get("preview_kind") == "image"]
    assert len(broken) == SIMGAD_CREATIVES
    assert all(r["preview_broken"] and not r["broken_unverified"] and not r.get("variation_used")
               and not r["headline"] for r in broken)
    row = row_of(res, broken_creative())
    assert row["copy_status"] == "preview_broken" and not row["headline"]
    assert row["image_file"] == f"images/{BROKEN_ID}.png"


def test_broken_preview_detail_blocked_retries_next_run(tmp_path):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    fake.detail_reply = response(302, b"", {"Location": "https://www.google.com/sorry/index"})
    clock = Clock()
    session = make_session(tmp_path, fake, clock)
    res = run_scrape(tmp_path, fake, session=session)
    assert res.failed == {}
    key = broken_key(tmp_path)
    first = extract_results(tmp_path)[key]
    assert first["preview_broken"] and first["broken_unverified"] and not first["variation_used"]
    assert row_of(res, broken_creative())["copy_status"] == "preview_broken"
    fake.detail_reply = None
    clock.now += 1000  # cooldown passed
    res = run_scrape(tmp_path, fake, session=session)
    assert res.failed == {}
    got = extract_results(tmp_path)[key]
    assert got["variation_used"] == 2 and not got.get("preview_broken") and not got.get("broken_unverified")
    assert row_of(res, broken_creative())["copy_status"] == "read"


def test_broken_preview_unparsable_detail_is_unverified(tmp_path):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    fake.detail_reply = response(body={"1": {}})  # no variations list: parse_detail raises ValueError
    res = run_scrape(tmp_path, fake)
    assert res.failed == {}
    got = extract_results(tmp_path)[broken_key(tmp_path)]
    assert got["broken_unverified"]


def test_vision_reruns_do_not_retry_a_confirmed_broken_preview(tmp_path):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    fake.detail_reply = response(body={"1": {"5": []}})  # no other Variation: the Broken Preview is confirmed
    run_scrape(tmp_path, fake)
    assert fake.calls["detail"] == 1
    assert not extract_results(tmp_path)[broken_key(tmp_path)]["broken_unverified"]
    codex_run = FakeCodexRun()
    for _ in range(2):
        res = run_scrape(tmp_path, fake, codex_runner=codex(run=codex_run), vision=True)
        assert res.failed == {}
    assert fake.calls["detail"] == 1  # no detail RPC on the --vision runs
    assert extract_results(tmp_path)[broken_key(tmp_path)]["preview_broken"]
    # Codex read every other image once (first --vision run) and nothing on the second
    assert codex_run.calls == SIMGAD_CREATIVES - 1


def test_variations_mode_does_not_use_the_fallback(tmp_path):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, variations=True)
    assert res.failed == {} and fake.calls["detail"] == 40  # the detail stage only
    row = row_of(res, broken_creative())
    assert row["copy_status"] == "preview_broken" and not row["headline"]
    results = extract_results(tmp_path, "anywhere_all_all_variations")
    assert not any(r.get("variation_used") for r in results.values())


class ErrorPageCodexRun(FakeCodexRun):
    """Codex reading the error page as if it were ad copy, as it did before Broken Previews were detected."""

    def __call__(self, argv, **kwargs):
        super().__call__(argv, **kwargs)
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({
            "headline": "500. That's an error.", "full_text": "There was an error. Please try again later.",
            "confidence": 0.9}))
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_old_row_with_error_page_copy_is_reextracted_once(tmp_path, monkeypatch):
    from adrenalin.tools.ads_transparency import copy as copy_mod

    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    with monkeypatch.context() as m:  # a run from before Broken Previews were detected
        m.setattr(copy_mod, "is_broken_preview", lambda text: False)
        run_scrape(tmp_path, fake, codex_runner=codex(run=ErrorPageCodexRun()))
    key = f"{broken_creative()}#1"
    old = extract_results(tmp_path)[key]
    assert "That" in old["headline"] + old["full_text"] and not old.get("preview_broken")
    assert fake.calls["detail"] == 0
    res = run_scrape(tmp_path, fake)
    got = extract_results(tmp_path)[key]
    assert got["variation_used"] == 2 and got["headline"] == "Big Bold Headline"
    assert row_of(res, broken_creative())["copy_status"] == "read"
    assert fake.calls["detail"] == 1
    run_scrape(tmp_path, fake)
    assert fake.calls["detail"] == 1  # not re-extracted again


COPY = {"headline": "Buy now"}


@pytest.mark.parametrize("status, result, held, want", [
    ("done", COPY, True, "not_sampled"),
    (None, None, False, "pending"),
    ("pending", None, False, "pending"),
    ("running", None, False, "pending"),
    ("failed", None, False, "pending"),
    ("done", {"preview_broken": True}, False, "preview_broken"),
    ("done", {"content_unparsed": True}, False, "unreadable"),
    ("done", {"brand": "A"}, False, "read"),
    ("done", {"headline": "H"}, False, "read"),
    ("done", {"description": "D"}, False, "read"),
    ("done", {"display_url": "a.com"}, False, "read"),
    ("done", {"preview_kind": "image", "full_text": "", "format": "image", "vision_pending": True}, False, "pending"),
    ("done", {"preview_kind": "image", "full_text": "", "format": "video", "vision_pending": True}, False, "pending"),
    ("done", {"preview_kind": "content_js"}, False, "no_copy"),
    ("done", {"preview_kind": "image", "full_text": "", "format": "image"}, False, "no_copy"),
    ("done", {"preview_kind": "image", "full_text": "", "format": "video"}, False, "no_copy"),
    ("done", {"preview_kind": "image", "full_text": "", "format": "text"}, False, "unreadable"),
    ("done", {"preview_kind": "image", "full_text": "stray words", "format": "image"}, False, "unreadable"),
])
def test_copy_status_rules(status, result, held, want):
    assert copy_status(status, result, held) == want


def test_infer_accounts_only_when_website_maps_to_one_account():
    rows = [{"website": w, "ads_account_id": a, "ad_group_id": "g" if a else ""}
            for w, a in [("a.com", "X"), ("a.com", ""), ("b.com", "X"), ("b.com", "Y"), ("b.com", "")]]
    infer_accounts(rows)
    assert [(r["ads_account_id"], r["ads_account_source"]) for r in rows] == [
        ("X", "exposed"), ("X", "inferred"), ("X", "exposed"), ("Y", "exposed"), ("", "")]
    assert [r["ad_group_id"] for r in rows] == ["g", "", "g", "g", ""]


def test_scrape_rows_have_status_and_account_columns(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle())
    assert CSV_FIELDS[-6:] == ("copy_status", "sample_weight", "sample_role", "ads_account_id", "ads_account_source", "ad_group_id")
    with res.csv_path.open(encoding="utf-8") as f:
        assert tuple(next(csv.reader(f)))[-5:] == CSV_FIELDS[-5:]
    rows = read_rows(res)
    assert rows and all(r["copy_status"] in COPY_STATUSES for r in rows)
    assert all(r["sample_weight"] == "1.0" for r in rows if r["copy_status"] not in ("not_sampled", "pending"))
    assert all(r["ads_account_source"] in ("exposed", "inferred", "") for r in rows)


def test_export_of_pre_change_run_dir(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    for key, r in q.results("extract"):
        q.complete("extract", key, {k: v for k, v in (r or {}).items()
                                    if k not in ("preview_kind", "content_unparsed", "preview_broken")})
    q.close()
    rows = read_rows(run_scrape(tmp_path, fake, stage="export"))
    assert rows and all(r["copy_status"] in COPY_STATUSES for r in rows)
    assert any(r["headline"] and r["copy_status"] == "read" for r in rows)


# -- sampled runs (Copy sample) ---------------------------------------------

@pytest.fixture
def small_limits(monkeypatch):
    """The fixture's 40 Creatives (Google's estimate: 600) count as a large Inventory."""
    monkeypatch.setattr(sampling, "FULL_READ_LIMIT", 10)
    monkeypatch.setattr(sampling, "HEAD_START", 5)


def summary_of(res):
    return json.loads(res.summary_path.read_text(encoding="utf-8"))


def stored_sampling(tmp_path, name="anywhere_all_all"):
    q = WorkQueue(run_dir(tmp_path, name) / "state.sqlite3")
    try:
        return json.loads(q.get_meta("sampling") or "null"), q.count("held", "pending")
    finally:
        q.close()


def group_of(row):
    account = preview_ids(row["preview_url"])[0] if "content.js" in row["preview_url"] else ""
    b = sampling.bucket_key(date.fromisoformat(row["first_shown"]), date.fromisoformat(row["last_shown"]),
                            None, None, date.today())
    return f"{b}|{account}"


def processed(rows):
    return [r for r in rows if r["copy_status"] not in ("not_sampled", "pending")]


def ocr_with_messages(pool=None):
    """An OCR fake whose headline differs per image (or per image modulo `pool`), so Messages don't all repeat."""
    def ocr(path):
        n = int(hashlib.sha1(Path(path).stem.encode()).hexdigest()[:8], 16)
        tag = n % pool if pool else n
        return OcrResult([
            OcrLine("Lascade", 0.95, (0.05, 0.05, 0.3, 0.05)),
            OcrLine("www.lascade.com", 0.95, (0.05, 0.12, 0.4, 0.05)),
            OcrLine(f"Headline number {tag}", 0.95, (0.05, 0.2, 0.8, 0.1)),
            OcrLine("A smaller description line.", 0.95, (0.05, 0.35, 0.45, 0.05)),
        ], "apple_vision")
    return ocr


def test_small_inventory_reads_everything(tmp_path):
    res = run_scrape(tmp_path, FakeGoogle())
    rows = read_rows(res)
    meta, held = stored_sampling(tmp_path)
    assert meta["p"] == 1.0 and held == 0
    assert len(rows) == 40 and len(processed(rows)) == 40
    assert not any(r["copy_status"] == "not_sampled" for r in rows)
    summary = summary_of(res)
    assert summary["listing_complete"] is True and "not_sampled" not in summary["copy_status"]


def test_large_inventory_samples_and_stops_at_coverage(tmp_path, small_limits):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake)
    rows = read_rows(res)
    meta, held = stored_sampling(tmp_path)
    assert meta == {"p": 5 / 600, "estimate": 600, "target": 0.95, "max_ads": None}
    assert len(rows) == 40 == len({r["creative_id"] for r in rows})
    sampled = processed(rows)
    assert 0 < len(sampled) < 40 and held == 40 - len(sampled)
    assert {r["copy_status"] for r in rows} - {"read", "no_copy", "not_sampled"} == set()
    summary = summary_of(res)
    assert summary["coverage"] is not None and summary["coverage"] >= 0.95
    assert res.coverage == summary["coverage"]
    assert summary["sampling"]["p"] == 5 / 600
    groups = Counter(group_of(r) for r in rows)
    assert {b["bucket"]: b["inventory"] for b in summary["groups"]} == dict(groups)
    for b in summary["groups"]:
        assert b["processed"] >= min(sampling.MIN_PER_BUCKET, b["inventory"])
        assert set(b) == {"bucket", "inventory", "processed", "messages", "coverage"}
    assert fake.calls["simgad"] < SIMGAD_CREATIVES


def test_max_ads_caps_reads(tmp_path, small_limits):
    fake = FakeGoogle()
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, max_ads=32), session=make_session(tmp_path, fake),
                 ocr=ocr_with_messages(), codex=codex(), log=lambda m: None, poll=POLL)
    rows = read_rows(res)
    # every bucket's minimum (29 Creatives here) comes first, then reads stop at the cap
    assert len(processed(rows)) == 32
    assert summary_of(res)["coverage"] < 0.95


def test_read_all_flag_overrides_sampling(tmp_path, small_limits):
    res = run_scrape(tmp_path, FakeGoogle(), read_all=True)
    rows = read_rows(res)
    meta, held = stored_sampling(tmp_path)
    assert meta["p"] == 1.0 and held == 0
    assert len(processed(rows)) == 40
    assert all(r["sample_weight"] == "1.0" for r in rows)


def test_read_all_rerun_reads_the_held_creatives(tmp_path, small_limits):
    fake = FakeGoogle()
    first = read_rows(run_scrape(tmp_path, fake))
    assert any(r["copy_status"] == "not_sampled" for r in first)
    rows = read_rows(run_scrape(tmp_path, fake, read_all=True))
    meta, held = stored_sampling(tmp_path)
    assert meta["p"] == 5 / 600 and held == 0
    assert len(rows) == 40 and len(processed(rows)) == 40


def test_rerun_with_higher_coverage_continues(tmp_path, small_limits):
    fake = FakeGoogle()
    ocr = ocr_with_messages(pool=20)

    def run(target):
        return scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, coverage=target), session=make_session(tmp_path, fake),
                      ocr=ocr, codex=codex(), log=lambda m: None, poll=POLL)

    first = read_rows(res1 := run(0.3))
    coverage1 = summary_of(res1)["coverage"]  # the rerun rewrites summary.json
    meta1, _ = stored_sampling(tmp_path)
    simgad1 = fake.calls["simgad"]
    second = read_rows(res2 := run(0.99))
    meta2, _ = stored_sampling(tmp_path)
    assert meta2["p"] == meta1["p"] == 5 / 600 and meta2["target"] == 0.99
    assert len(second) == 40 == len({r["creative_id"] for r in second})  # the relist re-held nothing
    read1 = {r["creative_id"]: r for r in processed(first)}
    read2 = {r["creative_id"]: r for r in processed(second)}
    assert set(read1) < set(read2)
    assert all(read2[c]["headline"] == r["headline"] for c, r in read1.items())
    new_images = sum(1 for c in set(read2) - set(read1) if read2[c]["image_file"])
    assert fake.calls["simgad"] - simgad1 == new_images
    assert 0.3 <= coverage1 < summary_of(res2)["coverage"]


def test_missing_estimate_reads_everything(tmp_path, small_limits):
    page = single_page()
    del page["4"], page["5"]
    res = run_scrape(tmp_path, FakeGoogle(search=lambda req: page))
    meta, held = stored_sampling(tmp_path)
    assert meta["p"] == 1.0 and held == 0
    assert len(processed(read_rows(res))) == 40


def test_variations_with_sampling(tmp_path, small_limits):
    fake = FakeGoogle()
    res = run_scrape(tmp_path, fake, variations=True)
    rows = read_rows(res)
    by_creative: dict[str, list[dict]] = {}
    for r in rows:
        by_creative.setdefault(r["creative_id"], []).append(r)
    assert len(by_creative) == 40
    admitted = {c for c, rs in by_creative.items() if rs[0]["copy_status"] != "not_sampled"}
    assert 0 < len(admitted) < 40 and fake.calls["detail"] == len(admitted)
    for c, rs in by_creative.items():
        assert len(rs) == (2 if c in admitted else 1)
        assert len({r["copy_status"] for r in rs}) == 1 and len({r["sample_weight"] for r in rs}) == 1
    summary = summary_of(res)
    assert sum(b["inventory"] for b in summary["groups"]) == 40
    assert sum(b["processed"] for b in summary["groups"]) == len(admitted)


def test_sample_weight_per_bucket(tmp_path, small_limits):
    res = run_scrape(tmp_path, FakeGoogle())
    rows = read_rows(res)
    assert all(r["sample_weight"] == "" for r in rows if r["copy_status"] == "not_sampled")
    weights: Counter = Counter()
    for r in processed(rows):
        weights[group_of(r)] += float(r["sample_weight"])
    buckets = summary_of(res)["groups"]
    assert buckets
    for b in buckets:
        assert weights[b["bucket"]] == pytest.approx(b["inventory"], abs=1e-6)


def test_head_start_admits_at_random_and_holds_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(sampling, "FULL_READ_LIMIT", 10)
    monkeypatch.setattr(sampling, "HEAD_START", 300)  # p = 0.5
    run_scrape(tmp_path, FakeGoogle(), stage="list")
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    fetch = {i.payload["creative_id"] for i in q.items("fetch")}
    held = {i.payload["creative_id"] for i in q.items("held")}
    q.close()
    ids = {c["2"] for c in SEARCH["1"]}
    assert fetch == {c for c in ids if sampling.admitted(c, 0.5)} and held == ids - fetch
    assert fetch and held


def test_resumed_listing_without_page_estimate_uses_the_stored_one(tmp_path, small_limits):
    p1, p2 = two_pages()
    sorry = response(302, b"", headers={"Location": "https://www.google.com/sorry/index"})
    state = {"blocked": True}
    fake = FakeGoogle(search=lambda req: (sorry if state["blocked"] else p2) if req.get("4") == "CUR1" else p1)
    run_scrape(tmp_path, fake)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    meta = json.loads(q.get_meta("sampling"))
    del meta["p"]  # as if the run stopped before p was stored
    q.set_meta("sampling", json.dumps(meta))
    q.close()
    state["blocked"] = False
    fake.search = lambda req: p2 if req.get("4") == "CUR1" else sorry  # only the cursor page, which has no estimate
    run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock=Clock(2_000_000.0)))
    assert stored_sampling(tmp_path)[0]["p"] == 5 / 600


# -- review fixes -------------------------------------------------------------

def no_copy_ocr(path):
    return OcrResult([], "apple_vision")


def empty_codex():
    def run(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps({
            "brand": "", "display_url": "", "headline": "", "description": "", "cta": "", "full_text": "",
            "confidence": 0.0}))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return codex(run=run)


def test_no_readable_messages_stops_after_the_minimums(tmp_path, small_limits):
    page = single_page()
    page["1"] = [c for c in page["1"] if "simgad" in json.dumps(c["3"])]  # image Previews only
    logs = []
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path), session=make_session(tmp_path, FakeGoogle(search=lambda r: page)),
                 ocr=no_copy_ocr, codex=empty_codex(), log=logs.append, poll=POLL)
    rows = read_rows(res)
    assert len(rows) == SIMGAD_CREATIVES and not any(r["headline"] for r in rows)
    # every group's minimum, then stop: Coverage can't be estimated, so reading on would read everything
    floor = sum(min(sampling.MIN_PER_BUCKET, b["inventory"]) for b in summary_of(res)["groups"])
    assert len(processed(rows)) == floor < SIMGAD_CREATIVES
    assert any(f"Coverage unknown: no readable Messages after {floor} Creatives" in m for m in logs)


def _queue_with_groups(tmp_path):
    """One group: two read Creatives with different Messages (Coverage 0), two read without copy, five held."""
    q = WorkQueue(tmp_path / "state.sqlite3")
    fields = {"format": "image", "first_shown": "2026-10-01", "last_shown": "2026-10-02", "days_shown": 2,
              "variation": 1, "preview": {"kind": "image", "url": "https://tpc/simgad/1", "width": 1, "height": 1}}
    for c, result in [("A1", {"headline": "One"}), ("A2", {"headline": "Two"}),
                      ("A3", {"preview_kind": "content_js"}), ("A4", {"preview_kind": "content_js"})]:
        q.put("fetch", f"{c}#1", {**fields, "creative_id": c})
        q.complete("fetch", f"{c}#1", {})
        q.put("extract", f"{c}#1", {})
        q.complete("extract", f"{c}#1", result)
    for c in ("H1", "H2", "H3", "H4", "H5"):
        q.put("held", f"{c}#1", {**fields, "creative_id": c})
    q.set_meta("list_done", "2026-11-20")
    return q


def _settings():
    return sample_queue.RoundSettings(False, Query(AR, None, None, None), date(2026, 11, 21), False, 0.95, None)


def test_round_stops_when_last_novelty_round_found_no_message(tmp_path):
    q = _queue_with_groups(tmp_path)
    logs = []
    item = Item("sample", "round:3", {"admitted": ["A3", "A4"], "novelty": True}, None, "running", None)
    out = sample_queue.run_round(q, item, _settings(), logs.append)
    assert out["admitted"] == 0 and q.count("held", "pending") == 5 and q.backlog("sample") == 0
    assert any("no readable Message" in m for m in logs)
    # a round after one that did find Messages keeps sampling
    item = Item("sample", "round:4", {"admitted": ["A1", "A4"], "novelty": True}, None, "running", None)
    assert sample_queue.run_round(q, item, _settings(), logs.append)["admitted"] > 0
    q.close()


def test_max_ads_caps_the_head_start(tmp_path, monkeypatch):
    monkeypatch.setattr(sampling, "FULL_READ_LIMIT", 10)
    monkeypatch.setattr(sampling, "HEAD_START", 600)  # alone, it would admit all 40 while listing
    res = scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, max_ads=32), session=make_session(tmp_path, FakeGoogle()),
                 ocr=ocr_with_messages(), codex=codex(), log=lambda m: None, poll=POLL)
    meta, _ = stored_sampling(tmp_path)
    assert meta["p"] == 32 / 600
    assert len(processed(read_rows(res))) == 32


def test_variations_weights_follow_variation_1(tmp_path, small_limits):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, variations=True)
    name = "anywhere_all_all_variations"
    q = WorkQueue(run_dir(tmp_path, name) / "state.sqlite3")
    read = sorted({k.partition("#")[0] for k, _ in q.results("extract")})
    a, b, c = read[:3]
    q.requeue("extract", [f"{a}#2"])  # A: Variation 1 read, Variation 2 pending
    q.complete("extract", f"{b}#2", {"preview_kind": "content_js"})  # B: Variation 2 has no copy
    q.requeue("extract", [f"{c}#1"])  # C: Variation 1 pending, Variation 2 read
    q.close()
    rows = read_rows(run_scrape(tmp_path, fake, variations=True, stage="export"))
    by = {}
    for r in rows:
        by.setdefault(r["creative_id"], {})[r["variation"]] = r
    assert by[a]["2"]["copy_status"] == "pending" and by[b]["2"]["copy_status"] == "no_copy"
    for cr in (a, b):
        assert by[cr]["1"]["sample_weight"] == by[cr]["2"]["sample_weight"] != ""
    assert by[c]["1"]["sample_weight"] == by[c]["2"]["sample_weight"] == ""
    # The group comes from the listed Preview, which a Variation row doesn't show.
    q = WorkQueue(run_dir(tmp_path, name) / "state.sqlite3")
    group = sample_queue.sample_groups(q, True, Query(AR, None, None, None), date.today()).group_of
    q.close()
    weights: Counter = Counter()
    for r in rows:
        if r["variation"] == "1" and r["sample_weight"]:
            weights[group[r["creative_id"]]] += float(r["sample_weight"])
    res = run_scrape(tmp_path, fake, variations=True, stage="export")
    buckets = [b for b in summary_of(res)["groups"] if b["processed"]]
    assert buckets and res.rows == len(rows)
    for bucket in buckets:
        assert weights[bucket["bucket"]] == pytest.approx(bucket["inventory"], abs=1e-6)


def test_round_left_pending_waits_for_the_relist(tmp_path, small_limits):
    fake = FakeGoogle()
    ocr = ocr_with_messages(pool=20)

    def run(target):
        return scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, coverage=target), session=make_session(tmp_path, fake),
                      ocr=ocr, codex=codex(), log=lambda m: None, poll=POLL)

    first = len(processed(read_rows(run(0.3))))
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    q.put("sample", "round:99", {})  # left pending by an interrupted run
    q.close()
    second = len(processed(read_rows(run(0.99))))  # an open window: the rerun relists first
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    results = [r for _, r in q.results("sample")]
    q.close()
    assert not any((r or {}).get("deferred") for r in results)  # no round ran while the Inventory was relisted
    assert second > first


def test_progress_total_is_the_listed_inventory_on_sampled_runs(tmp_path):
    q = WorkQueue(tmp_path / "state.sqlite3")
    for k in ("A#1", "B#1"):
        q.put("fetch", k, {})
    for k in ("C#1", "D#1", "E#1"):
        q.put("held", k, {})
    q.set_meta("list_done", "2026-11-20")
    q.set_meta("sampling", json.dumps({"p": 0.1}))
    creatives = _progress_bars(q, ["list", "sample", "fetch"])[0]
    assert creatives.label == "inventory"
    assert creatives.count(q.counts()) == (5, 5)
    copy = _progress_bars(q, ["list", "sample", "fetch"])[1]
    assert copy.count(q.counts()) == (0, 2)
    q.put("fetch", "F#1", {})
    assert copy.count(q.counts()) == (0, 3)
    q.close()


def test_stage_list_then_full_run_samples(tmp_path, small_limits):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake, stage="list")
    assert fake.calls["simgad"] == 0
    rows = read_rows(run_scrape(tmp_path, fake))
    assert len(rows) == 40 == len({r["creative_id"] for r in rows})
    assert 29 <= len(processed(rows)) < 40 and not any(r["copy_status"] == "pending" for r in rows)


def test_rerun_of_a_full_read_stays_a_full_read(tmp_path, monkeypatch):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    monkeypatch.setattr(sampling, "FULL_READ_LIMIT", 10)  # the estimate (600) would now mean sampling
    monkeypatch.setattr(sampling, "HEAD_START", 5)
    rows = read_rows(res := run_scrape(tmp_path, fake))  # open window: relists
    meta, held = stored_sampling(tmp_path)
    assert meta["p"] == 1.0 and held == 0
    assert len(processed(rows)) == 40 and all(r["sample_weight"] == "1.0" for r in rows)
    assert summary_of(res)["sampling"] is None


def test_round_during_a_failed_listing_is_deferred_and_seeded_again(tmp_path):
    q = _queue_with_groups(tmp_path)
    q.set_meta("list_done", "")  # the listing did not finish this run
    q.put("sample", "round:0", {})
    item = q.claim("sample")
    assert sample_queue.run_round(q, item, _settings(), lambda m: None) == {"deferred": True}
    q.complete("sample", "round:0", {"deferred": True})
    assert q.count("held", "pending") == 5 and q.backlog("sample") == 0
    q.set_meta("list_done", "2026-11-20")  # a later run finishes the listing, and list_page seeds a round
    sample_queue.seed_round(q)
    assert [i.key for i in q.items("sample", "pending")] == ["round:1"]
    q.close()


def test_max_ads_samples_an_inventory_within_the_full_read_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(sampling, "FULL_READ_LIMIT", 2000)  # the estimate (600) alone would mean a full read
    res = run_scrape(tmp_path, FakeGoogle(), max_ads=10)
    rows = read_rows(res)
    meta, held = stored_sampling(tmp_path)
    assert meta["p"] == 10 / 600 and held > 0
    assert any(r["copy_status"] == "not_sampled" for r in rows)
    floor = sum(min(sampling.MIN_PER_BUCKET, b["inventory"]) for b in summary_of(res)["groups"])
    assert len(processed(rows)) <= 10 + floor


# -- final review fixes ---------------------------------------------------------

def _resume_without_any_estimate(tmp_path, page1):
    """A run blocked after page 1, then stripped of every stored estimate (a queue from before sampling), resumed
    on the cursor page, which carries no estimate. `page1` is what a fresh page-1 search returns."""
    p1, p2 = two_pages()
    sorry = response(302, b"", headers={"Location": "https://www.google.com/sorry/index"})
    fake = FakeGoogle(search=lambda req: sorry if req.get("4") == "CUR1" else p1)
    run_scrape(tmp_path, fake)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    q.set_meta("sampling", "{}")
    q.set_meta("count_estimate", "")
    q.close()
    fake.search = lambda req: p2 if req.get("4") == "CUR1" else page1
    fake.calls.clear()
    run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock=Clock(2_000_000.0)))
    return fake


def test_resumed_cursor_page_without_any_estimate_reads_it_from_page_1(tmp_path, small_limits):
    p1, _ = two_pages()
    fake = _resume_without_any_estimate(tmp_path, p1)
    assert stored_sampling(tmp_path)[0]["p"] == 5 / 600
    assert fake.calls["search"] == 2  # the cursor page and one page-1 search for the estimate


def test_resumed_cursor_page_without_any_estimate_reads_all_if_page_1_has_none(tmp_path, small_limits):
    p1, _ = two_pages()
    del p1["4"], p1["5"]
    _resume_without_any_estimate(tmp_path, p1)
    assert stored_sampling(tmp_path)[0]["p"] == 1.0


def test_variations_creative_whose_detail_failed_still_has_a_row(tmp_path):
    fake = FakeGoogle()
    fake.detail_reply = response(body={"1": {}})  # unparsable: every detail item fails
    for run in range(3):
        res = run_scrape(tmp_path, fake, variations=True)
        rows = read_rows(res)
        assert len(rows) == 40 and {r["variation"] for r in rows} == {"1"}
        # failed items are retried on the next run; after the last attempt reading has failed for good
        assert {r["copy_status"] for r in rows} == {"unreadable" if run == 2 else "pending"}
        assert res.listed == summary_of(res)["listed"] == 40


def test_fetch_failed_on_every_retry_is_unreadable(tmp_path):
    fake = FakeGoogle()
    fake.fail_simgad = 10 ** 6
    for run in range(3):
        rows = read_rows(run_scrape(tmp_path, fake))
        images = [r for r in rows if "simgad" in r["preview_url"]]
        assert len(images) == SIMGAD_CREATIVES
        assert {r["copy_status"] for r in images} == {"unreadable" if run == 2 else "pending"}


def test_exhausted_reads_count_as_processed_in_the_copy_sample(tmp_path):
    q = _queue_with_groups(tmp_path)
    q.put("fetch", "A5#1", {**q.items("fetch")[0].payload, "creative_id": "A5"})
    for _ in range(3):
        q.claim("fetch")
        q.fail("fetch", "A5#1", "HTTPError")
        q.close()
        q = WorkQueue(tmp_path / "state.sqlite3")
    tally = sample_queue.sample_groups(q, False, Query(AR, None, None, None), date(2026, 11, 21))
    assert "A5" in tally.processed and tally.message["A5"] == ""
    q.close()


def test_stored_error_page_copy_is_preview_broken():
    error_page = {"headline": "500. That's an error.", "full_text": "There was an error. Please try again later.",
                  "preview_kind": "image", "format": "text"}
    assert copy_status("done", error_page) == "preview_broken"
    assert sample_queue.message_of("done", error_page, "text") == ""


def test_old_unparsed_and_store_badge_rows_are_reextracted_once(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    js = [k for k, r in q.results("extract") if (r or {}).get("preview_kind") == "content_js"]
    good = {k: r for k, r in q.results("extract")}
    old_unparsed, old_badge = js[0], js[1]
    # rows of a run from before the content.js parser fixes (no parser version stored)
    q.complete("extract", old_unparsed, {"preview_kind": "content_js", "content_unparsed": True})
    # a row from before preview_kind was stored
    stale = {k: v for k, v in good[old_badge].items() if k not in ("content_version", "preview_kind")}
    q.complete("extract", old_badge, {**stale, "headline": "Google Play"})
    q.close()
    before = fake.calls["content_js"]
    run_scrape(tmp_path, fake)
    got = extract_results(tmp_path)
    for k in (old_unparsed, old_badge):
        assert got[k]["headline"] == good[k]["headline"] and not got[k]["content_unparsed"]
    assert fake.calls["content_js"] == before + 2  # their scripts were deleted after the first extract
    run_scrape(tmp_path, fake)
    assert fake.calls["content_js"] == before + 2  # not re-extracted again


def _text_js_item(q):
    """The first done extract item of a text content.js Preview that was read with a headline and description."""
    return next(i for i in q.items("extract", "done")
                if (i.result or {}).get("preview_kind") == "content_js" and i.result.get("headline")
                and i.result.get("description") and i.payload["preview"]["url"] not in VIDEO_JS_URLS)


def _make_store_badge_row(tmp_path, store="Google Play"):
    """Turn a read text content.js row into one of an older parser that took the store badge as the headline.
    Returns (key, the correct result)."""
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        it = _text_js_item(q)
        old = {k: v for k, v in it.result.items() if k != "content_version"}
        q.complete("extract", it.key, {**old, "headline": store})
        return it.key, it.result
    finally:
        q.close()


def test_stale_reread_with_script_host_blocked_does_not_halt_image_extracts(tmp_path):
    fake, clock = FakeGoogle(), Clock()
    run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock))
    key, good = _make_store_badge_row(tmp_path)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    q.requeue("extract", [k for k, r in q.results("extract") if (r or {}).get("preview_kind") == "image"])
    q.close()
    fake.block_content_js = True
    before = fake.calls["content_js"]
    res = run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock))
    assert res.blocked_until is not None and fake.calls["content_js"] == before + 1
    images = [r for r in read_rows(res) if r["image_file"]]
    assert len(images) == SIMGAD_CREATIVES and all(r["extraction_method"] for r in images)
    row = row_of(res, key.split("#")[0])  # still the earlier copy, without the store badge
    assert (row["headline"], row["description"], row["copy_status"]) == ("", good["description"], "read")

    fake.block_content_js = False
    clock.now += 901
    res = run_scrape(tmp_path, fake, session=make_session(tmp_path, fake, clock))
    assert res.blocked_until is None and not res.failed
    assert row_of(res, key.split("#")[0])["headline"] == good["headline"]
    assert extract_results(tmp_path)[key]["content_version"] == good["content_version"]


def test_stale_reread_that_404s_keeps_earlier_copy_and_is_given_up(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    key, good = _make_store_badge_row(tmp_path)
    fake.content_status = 404
    before = fake.calls["content_js"]
    for n in range(1, 4):  # one attempt per run
        res = run_scrape(tmp_path, fake)
        row = row_of(res, key.split("#")[0])
        assert (row["headline"], row["description"], row["copy_status"]) == ("", good["description"], "read")
        assert fake.calls["content_js"] == before + n
    assert not res.failed  # given up: the row has its copy, nothing is left to retry
    res = run_scrape(tmp_path, fake)
    assert fake.calls["content_js"] == before + 3 and not res.failed
    assert row_of(res, key.split("#")[0])["copy_status"] == "read"


def test_old_unparsed_row_with_script_on_disk_is_reextracted_without_download(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        it = _text_js_item(q)
        q.complete("extract", it.key, {"preview_kind": "content_js", "content_unparsed": True})  # no version
    finally:
        q.close()
    path = run_dir(tmp_path) / it.payload["content_file"]
    path.write_bytes(CONTENT_TEXT)  # kept for inspection, as unparsed scripts are
    before = fake.calls["content_js"]
    res = run_scrape(tmp_path, fake)
    assert fake.calls["content_js"] == before and not res.failed
    assert row_of(res, it.key.split("#")[0])["headline"] == it.result["headline"]
    assert not path.exists()


def test_stale_reread_keeps_earlier_copy_until_extract_runs(tmp_path):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    key, good = _make_store_badge_row(tmp_path)
    before = fake.calls["content_js"]
    res = run_scrape(tmp_path, fake, stage="fetch")  # downloads the script again, but extract doesn't run
    assert fake.calls["content_js"] == before + 1
    row = row_of(res, key.split("#")[0])
    assert (row["headline"], row["description"], row["copy_status"]) == ("", good["description"], "read")
    res = run_scrape(tmp_path, fake)
    assert fake.calls["content_js"] == before + 1  # read from the downloaded file
    assert row_of(res, key.split("#")[0])["headline"] == good["headline"]


@pytest.mark.parametrize("store", ["Google Play", "App Store"])
def test_stage_export_never_shows_a_store_badge_headline(tmp_path, store):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    key, good = _make_store_badge_row(tmp_path, store)
    res = run_scrape(tmp_path, fake, stage="export")
    assert not any(r["headline"] in ("Google Play", "App Store") for r in read_rows(res))
    row = row_of(res, key.split("#")[0])
    assert (row["headline"], row["description"], row["copy_status"]) == ("", good["description"], "read")


def test_next_round_reads_ahead_only_after_a_novelty_round(tmp_path):
    q = _queue_with_groups(tmp_path)
    assert sample_queue.round_lead(q) == 0  # no round waiting
    q.put("sample", "round:0", {})  # after the head start: every read must finish first
    assert sample_queue.round_lead(q) == 0
    q.complete("sample", "round:0", {})
    q.put("sample", "round:1", {"admitted": ["A1"], "novelty": True, "unjudged": [["A1"]]})
    assert sample_queue.round_lead(q) == sample_queue.ROUND_BATCH
    q.put("list", "page:", {"cursor": ""})  # a relist: the Inventory must be complete first
    assert sample_queue.round_lead(q) == 0
    q.close()


def test_round_logs_its_progress(tmp_path):
    q = _queue_with_groups(tmp_path)
    logs = []
    item = Item("sample", "round:4", {"admitted": ["A1"], "novelty": True}, None, "running", None)
    out = sample_queue.run_round(q, item, _settings(), logs.append)
    assert out["admitted"] == 5
    assert "sample: round 4: read 4, Coverage 0.000 (target 0.95), held 0" in logs
    q.close()


def _codex_capped(q, *creatives):
    fields = q.items("fetch")[0].payload
    for c in creatives:
        q.move("held", "fetch", [f"{c}#1"])
        q.complete("fetch", f"{c}#1", {})
        q.put("extract", f"{c}#1", {**fields, "creative_id": c})
        q.complete("extract", f"{c}#1", {"preview_kind": "image", "vision_pending": True})


def test_round_of_codex_capped_creatives_defers_instead_of_stopping(tmp_path):
    q = _queue_with_groups(tmp_path)
    _codex_capped(q, "H1", "H2")
    logs = []
    item = Item("sample", "round:5", {"admitted": ["H1", "H2"], "novelty": True}, None, "running", None)
    out = sample_queue.run_round(q, item, _settings(), logs.append)
    # no processed Creative to judge the round by: admit nothing this run, and don't claim it found no Message
    assert out["admitted"] == 0 and out["deferred"] and q.backlog("sample") == 0
    assert not any("no readable Message" in m for m in logs)
    q.close()


def test_round_still_being_read_is_judged_later(tmp_path):
    q = _queue_with_groups(tmp_path)
    q.move("held", "fetch", ["H1#1", "H2#1"])  # admitted by the last round, not read yet
    item = Item("sample", "round:5", {"admitted": ["H1", "H2"], "novelty": True}, None, "running", None)
    out = sample_queue.run_round(q, item, _settings(), lambda m: None)
    assert out["admitted"] == 3
    nxt = q.items("sample", "pending")[0].payload
    assert nxt["unjudged"] == [["H1", "H2"], nxt["admitted"]] and len(nxt["admitted"]) == 3
    q.close()


def test_round_judges_only_processed_creatives(tmp_path):
    q = _queue_with_groups(tmp_path)
    _codex_capped(q, "H1")
    item = Item("sample", "round:5", {"admitted": ["A3", "H1"], "novelty": True}, None, "running", None)
    logs = []
    out = sample_queue.run_round(q, item, _settings(), logs.append)
    assert out["admitted"] == 0 and not out.get("deferred")  # A3 was read and has no Message: stop
    assert any("no readable Message" in m for m in logs)
    q.close()


def test_cli_prints_incomplete_and_blocked_once(tmp_path, monkeypatch, capsys):
    from adrenalin.cli import main
    from adrenalin.tools.ads_transparency import api

    p1, _ = two_pages()
    fake = FakeGoogle(search=lambda req: SORRY if req.get("4") == "CUR1" else p1)
    real = api.scrape
    monkeypatch.setattr(api, "scrape", lambda options, **kw: real(
        options, session=make_session(tmp_path, fake), ocr=fake_ocr, codex=codex(), poll=POLL, **kw))
    assert main(["ads-transparency", AR, "--out", str(tmp_path)]) == 2
    err = capsys.readouterr().err.splitlines()
    assert sum(ln.startswith("INCOMPLETE:") for ln in err) == 1 and err[-1].startswith("INCOMPLETE:")
    assert sum(ln.startswith("blocked until") for ln in err) == 1
    assert any("list: blocked" in ln for ln in err)  # the stage's own log line is kept


def test_stage_export_keeps_the_sampling_options_and_stage_times(tmp_path, small_limits):
    fake = FakeGoogle()
    first = summary_of(run_scrape(tmp_path, fake, coverage=0.5, max_ads=30))
    assert first["stage_seconds"]
    again = summary_of(run_scrape(tmp_path, fake, stage="export"))
    assert (again["sampling"]["target"], again["sampling"]["max_ads"]) == (0.5, 30)
    assert again["stage_seconds"] == first["stage_seconds"]


@pytest.mark.parametrize("opts", [{"coverage": 0}, {"coverage": 1}, {"coverage": 1.5}, {"max_ads": 0}])
def test_scrape_options_reject_bad_coverage_and_max_ads(opts):
    with pytest.raises(ValueError):
        ScrapeOptions(AR, sampling_mode="novelty", **opts)


class BrokenOnlyCodexRun(FakeCodexRun):
    """Codex reading the Broken Preview as Google's error page and every other image as ad copy."""

    def __call__(self, argv, **kwargs):
        super().__call__(argv, **kwargs)
        if Path(argv[argv.index("-i") + 1]).stem == BROKEN_ID:
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps({
                "headline": "500. That's an error.", "full_text": "There was an error. Please try again later.",
                "confidence": 0.9}))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.mark.parametrize("vision", [False, True])
def test_broken_preview_with_unparsable_detail_is_confirmed_after_3_runs(tmp_path, vision):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    fake.detail_reply = response(body={"1": {}})  # permanently unparsable
    for _ in range(4):  # --vision reruns requeue unverified Broken Previews first: the count must survive that
        assert run_scrape(tmp_path, fake, vision=vision, codex_runner=codex(run=BrokenOnlyCodexRun())).failed == {}
    assert fake.calls["detail"] == 3
    got = extract_results(tmp_path)[broken_key(tmp_path)]
    assert got["preview_broken"] and not got["broken_unverified"]


def test_broken_preview_blocked_detail_does_not_use_up_the_checks(tmp_path):
    BROKEN_IDS.add(BROKEN_ID)
    fake = FakeGoogle()
    fake.detail_reply = SORRY
    clock = Clock()
    session = make_session(tmp_path, fake, clock)
    for _ in range(4):
        run_scrape(tmp_path, fake, session=session)
        clock.now += 1000
    assert extract_results(tmp_path)[broken_key(tmp_path)]["broken_unverified"]


def test_max_ads_below_the_group_minimums_warns(tmp_path, small_limits):
    logs = []
    scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, max_ads=5), session=make_session(tmp_path, FakeGoogle()),
           ocr=fake_ocr, codex=codex(), log=logs.append, poll=POLL)
    assert any(m.startswith("warning: --max-ads 5 is below the group minimums") for m in logs)


def test_rerun_with_a_different_max_ads_notes_the_kept_head_start(tmp_path, small_limits):
    fake = FakeGoogle()
    run_scrape(tmp_path, fake)
    logs = []
    scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, max_ads=3), session=make_session(tmp_path, fake), ocr=fake_ocr,
           codex=codex(), log=logs.append, poll=POLL)
    assert stored_sampling(tmp_path)[0]["p"] == 5 / 600
    assert any(m.startswith("note: the head start was set by this run folder's first run") for m in logs)
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    q.set_meta("count_estimate", "700")  # Google's estimate drifted; `p` was sized from 600
    q.close()
    logs.clear()
    scrape(ScrapeOptions(AR, sampling_mode="novelty", out=tmp_path, max_ads=5), session=make_session(tmp_path, fake), ocr=fake_ocr,
           codex=codex(), log=logs.append, poll=POLL)
    assert not any(m.startswith("note:") for m in logs)  # the same cap as the first run: nothing to note


def test_script_workers_overlap_downloads(tmp_path):
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    started = 0
    class ParallelGoogle(FakeGoogle):
        def __call__(self, method, url, **kwargs):
            nonlocal started
            if 'content.js' in url:
                with lock:
                    started += 1
                    first = started <= 3
                if first:
                    barrier.wait(timeout=5)
            return super().__call__(method, url, **kwargs)
    result = run_scrape(tmp_path, ParallelGoogle())
    assert started >= 3
    assert not result.failed


def test_script_disconnect_recovers_during_run(tmp_path):
    lock = threading.Lock()
    disconnected = False
    class FlakyGoogle(FakeGoogle):
        def __call__(self, method, url, **kwargs):
            nonlocal disconnected
            if 'content.js' in url:
                with lock:
                    if not disconnected:
                        disconnected = True
                        raise requests.ConnectionError('Remote end closed connection without response')
            return super().__call__(method, url, **kwargs)
    result = run_scrape(tmp_path, FlakyGoogle())
    assert disconnected
    assert not result.failed


def test_progress_counts_variations_as_copy_not_inventory(tmp_path):
    q = WorkQueue(tmp_path / 'progress.sqlite3')
    try:
        q.put('detail', 'A', {})
        q.put('fetch', 'A#1', {})
        q.put('fetch', 'A#2', {})
        q.set_meta('list_done', '2026-11-24')
        bars = _progress_bars(q, ['list', 'detail', 'fetch'])
        assert bars[0].count(q.counts()) == (1, 1)
        assert bars[1].count(q.counts()) == (0, 2)
    finally:
        q.close()
