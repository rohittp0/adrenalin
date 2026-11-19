import copy
import csv
import json
import subprocess
import threading
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
import requests

from adrenalin.common.http import Cooldown, PacedSession
from adrenalin.common.ocr import OcrLine, OcrResult
from adrenalin.common.ocr.codex import CodexRunner
from adrenalin.common.output import EntityCollision
from adrenalin.common.pipeline import WorkQueue
from adrenalin.tools.ads_transparency.api import CSV_FIELDS, ScrapeOptions, list_creatives, scrape

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
                return response(body=DETAIL, url=url)
            if "simgad" in url:
                self.calls["simgad"] += 1
                if self.fail_simgad:
                    self.fail_simgad -= 1
                    return response(500, b"oops", url=url)
                return response(body=SIMGAD, headers={"Content-Type": "image/png"}, url=url)
            if "content.js" in url:
                self.calls["content_js"] += 1
                body = CONTENT_VIDEO if url in VIDEO_JS_URLS else CONTENT_TEXT
                return response(body=body, headers={"Content-Type": "text/javascript"}, url=url)
            if "oembed" in url:
                self.calls["oembed"] += 1
                if self.oembed_status != 200:
                    return response(self.oembed_status, b"Unauthorized", url=url)
                return response(body=OEMBED, headers={"Content-Type": "application/json"}, url=url)
        raise AssertionError(f"unexpected request {method} {url}")


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_session(tmp_path, fake, clock=None):
    clock = clock or Clock()
    return PacedSession(Cooldown(tmp_path / "test-cooldown.sqlite3", clock=clock), intervals={},
                        transport=fake, sleep=lambda s: None, clock=clock)


def fake_ocr(path):
    lines = [
        OcrLine("Lascade", 0.95, (0.05, 0.05, 0.3, 0.05)),
        OcrLine("www.lascade.com", 0.95, (0.05, 0.12, 0.4, 0.05)),
        OcrLine("Big Bold Headline", 0.95, (0.05, 0.2, 0.8, 0.1)),
        OcrLine("A smaller description line.", 0.95, (0.05, 0.35, 0.8, 0.05)),
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


def test_expired_cursor_restarts_listing(tmp_path):
    p1, p2 = two_pages()
    state = {"expired": 1}

    def search(req):
        if req.get("4") == "CUR1":
            if state["expired"]:
                state["expired"] -= 1
                return response(400, b"<html><body>Bad Request</body></html>", {"Content-Type": "text/html"})
            return p2
        return p1

    fake = FakeGoogle(search=search)
    res = run_scrape(tmp_path, fake)
    rows = read_rows(res)
    assert len(rows) == 40 == len({r["creative_id"] for r in rows})
    assert sum(1 for b in fake.search_bodies if b.get("4") == "CUR1") == 2
    assert res.failed == {}
    q = WorkQueue(run_dir(tmp_path) / "state.sqlite3")
    try:
        assert q.get_meta("list_done") == date.today().isoformat()
    finally:
        q.close()


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
    # still cooling down: nothing more is listed
    assert run_scrape(tmp_path, fake, session=session).blocked_until is not None
    clock.now += 1000
    res2 = run_scrape(tmp_path, fake, session=session)
    assert res2.blocked_until is None and res2.rows == 40 and res2.failed == {}


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
