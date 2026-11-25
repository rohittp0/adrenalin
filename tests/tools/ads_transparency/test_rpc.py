import json
from datetime import date
from pathlib import Path

import pytest
import requests

from adrenalin.tools.ads_transparency.rpc import (
    DETAIL_URL,
    SEARCH_URL,
    ShownCountry,
    PaginationLoop,
    Query,
    TransparencyClient,
    build_search_request,
    parse_detail,
    parse_shown_countries,
    preview_ids,
    parse_search_page,
    transparency_url,
)

FIX = Path(__file__).parents[2] / "fixtures" / "ads_transparency"


def _load(name):
    return json.loads((FIX / name).read_text())


class FakeSession:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def post(self, url, params=None, data=None, headers=None, **kw):
        self.calls.append((url, params, data, headers))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        status, body = body if isinstance(body, tuple) else (200, body)
        r = requests.Response()
        r.status_code = status
        r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return r


def test_build_search_request_with_region_and_dates():
    q = Query("AR1", 2356, date(2026, 11, 12), date(2026, 11, 18))
    r = build_search_request(q, cursor="abc", page_size=100)
    assert r["2"] == 100 and r["7"] == {"1": 1, "2": 0, "3": 2356}
    assert r["3"]["12"] == {"1": "", "2": True} and r["3"]["13"] == {"1": ["AR1"]}
    assert r["3"]["8"] == [2356] and r["3"]["6"] == 20261112 and r["3"]["7"] == 20261119
    assert r["4"] == "abc"


def test_build_search_request_anywhere_all_time_omits_filters():
    r = build_search_request(Query("AR1", None, None, None), cursor=None)
    assert not {"8", "6", "7"} & set(r["3"]) and "4" not in r


def test_parse_search_page_fixture():
    p = parse_search_page(_load("search_page.json"))
    assert len(p.creatives) == 40 and p.next_cursor and p.skipped == 0
    assert (p.count_low, p.count_high) == (500, 600)
    assert {c.format for c in p.creatives} == {"text", "image", "video"}
    assert all(c.advertiser_name == "Lascade LLC" for c in p.creatives)
    img = next(c for c in p.creatives if c.preview and c.preview.kind == "image")
    assert img.preview.image_id.isdigit() and img.preview.width
    assert img.preview.url.startswith("https://tpc.googlesyndication.com/archive/simgad/")
    js = next(c for c in p.creatives if c.preview and c.preview.kind == "content_js")
    assert js.preview.image_id is None
    assert img.first_shown.tzinfo is not None and img.days_shown == 182


def test_parse_search_page_skips_malformed_creative():
    d = _load("search_page.json")
    del d["1"][0]["2"]
    p = parse_search_page(d)
    assert p.skipped == 1 and len(p.creatives) == 39


def test_parse_search_page_empty_is_valid():
    p = parse_search_page({})
    assert p.creatives == [] and p.next_cursor is None


def test_parse_detail_fixture_dedupes_to_2_variations():
    v = parse_detail(_load("creative_detail.json"))
    assert [p.image_id for p in v] == ["877210798417700731", "10849098364905980222"]


def test_parse_detail_dedupes_content_js_ignoring_params():
    base = "https://x/content.js?a=1&creativeId=9"
    mk = lambda u: {"3": {"1": {"4": u}}}
    data = {"1": {"5": [mk(base + "&htmlParentId=p1&responseCallback=c1"),
                        mk(base + "&htmlParentId=p2&responseCallback=c2"),
                        mk(base + "&other=1")]}}
    assert len(parse_detail(data)) == 2


def test_transparency_url():
    assert (
        transparency_url("AR1", "CR2", "IN")
        == "https://adstransparency.google.com/advertiser/AR1/creative/CR2?region=IN"
    )


def test_client_search_posts_form_and_detects_cursor_loop():
    s = FakeSession([_load("search_page.json")])
    c = TransparencyClient(s)
    q = Query("AR1", None, None, None)
    p = c.search(q)
    url, params, data, headers = s.calls[0]
    assert url == SEARCH_URL and params == {"authuser": "0"}
    assert data["f.req"].startswith("{") and " " not in data["f.req"].replace("Lascade LLC", "")
    assert headers["X-Same-Domain"] == "1"
    assert headers["Origin"] == "https://adstransparency.google.com"
    assert headers["Referer"] == "https://adstransparency.google.com/advertiser/AR1"
    assert p.next_cursor
    with pytest.raises(PaginationLoop):
        list(c.iter_pages(q))


def test_iter_pages_ends_on_no_cursor():
    d = _load("search_page.json")
    d2 = {"1": d["1"][:2]}
    s = FakeSession([d, d2])
    d["2"] = "c1"
    pages = list(TransparencyClient(s).iter_pages(Query("AR1", None, None, None)))
    assert len(pages) == 2


def test_search_rejects_bad_responses():
    q = Query("AR1", None, None, None)
    with pytest.raises(ValueError):
        TransparencyClient(FakeSession([b"<html>"])).search(q)
    with pytest.raises(ValueError):
        TransparencyClient(FakeSession([{"1": "x"}])).search(q)


def test_variations_posts_detail():
    s = FakeSession([_load("creative_detail.json")])
    v = TransparencyClient(s).variations("AR1", "CR2")
    assert len(v) == 2
    assert s.calls[0][0] == DETAIL_URL
    assert json.loads(s.calls[0][2]["f.req"]) == {
        "1": "AR1", "2": "CR2", "5": {"1": 1, "2": 0, "3": 2356}}


def test_advertiser_name_falls_back_to_unfiltered_query():
    s = FakeSession([{"1": []}, _load("search_page.json")])
    c = TransparencyClient(s)
    assert c.advertiser_name(Query("AR1", 2356, date(2026, 1, 1), date(2026, 1, 2))) == "Lascade LLC"
    assert "8" not in json.loads(s.calls[1][2]["f.req"])["3"]


def test_advertiser_name_none_when_both_empty():
    s = FakeSession([{"1": []}])
    assert TransparencyClient(s).advertiser_name(Query("AR1", 2356, None, None)) is None


def test_parse_search_page_skips_out_of_range_epoch():
    d = _load("search_page.json")
    d["1"][0]["6"] = {"1": "99999999999999999", "2": 0}
    p = parse_search_page(d)
    assert p.skipped == 1 and len(p.creatives) == 39


@pytest.mark.parametrize("bad", ["", 5, None])
def test_parse_search_page_skips_non_string_creative_id(bad):
    d = _load("search_page.json")
    d["1"][0]["2"] = bad
    assert parse_search_page(d).skipped == 1


@pytest.mark.parametrize("bad", [{}, {"1": "x"}, {"1": {"5": "x"}}, {"1": {}}])
def test_parse_detail_raises_value_error_on_bad_shape(bad):
    with pytest.raises(ValueError):
        parse_detail(bad)


def test_advertiser_name_unfiltered_query_makes_one_request():
    s = FakeSession([{"1": []}])
    assert TransparencyClient(s).advertiser_name(Query("AR1", None, None, None)) is None
    assert len(s.calls) == 1


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_non_200_json_response_raises_instead_of_an_empty_page(status):
    from adrenalin.tools.ads_transparency.rpc import RpcError

    q = Query("AR1", None, None, None)
    with pytest.raises(RpcError, match=f"HTTP {status}"):
        TransparencyClient(FakeSession([(status, {})])).search(q)
    assert issubclass(RpcError, ValueError)  # a rejected resumed cursor restarts the listing


def test_viewer_geo_context_is_named():
    from adrenalin.tools.ads_transparency.rpc import VIEWER_GEO_ID

    req = build_search_request(Query("AR1", None, None, None), None)
    assert req["7"] == {"1": 1, "2": 0, "3": VIEWER_GEO_ID}


def test_preview_ids_reads_account_and_ad_group():
    url = ("https://displayads-formats.googleusercontent.com/ads/preview/content.js?client=ads-integrity-transparency"
           "&obfuscatedCustomerId=6119286040&creativeId=1&adGroupId=196658869119&overlay=x")
    assert preview_ids(url) == ("6119286040", "196658869119")
    assert preview_ids("https://tpc.googlesyndication.com/simgad/123") == ("", "")


@pytest.mark.parametrize('failures', [1, 3])
def test_rpc_timeout_retries_same_cursor_bounded(failures):
    class Session:
        def __init__(self):
            self.calls = []
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) <= failures:
                raise requests.Timeout('signal timed out')
            r = requests.Response()
            r.status_code = 200
            r._content = b'{"1": []}'
            return r
    session = Session()
    client = TransparencyClient(session)
    query = Query('AR123', None, None, None)
    if failures == 3:
        with pytest.raises(requests.Timeout):
            client.search(query, 'saved-cursor')
    else:
        assert client.search(query, 'saved-cursor').creatives == []
    assert len(session.calls) == min(failures + 1, 3)
    assert all(call == session.calls[0] for call in session.calls)


def test_parse_shown_countries_from_video_fixture():
    raw = _load("creative_detail_video.json")["1"]["17"]
    got = parse_shown_countries(raw)
    assert got[0] == ShownCountry("NZ", date(2026, 11, 19), None, None, None)
    de = next(c for c in got if c.country == "DE")
    assert (de.first_shown, de.impressions_min, de.impressions_max) == (date(2026, 3, 22), 0, 1000)


def test_parse_shown_countries_reads_impression_range():
    [c] = parse_shown_countries([{"1": 2276, "2": "10000", "3": "15000", "4": 20250421, "5": 20261122,
                                  "7": 20250421}])
    assert (c.impressions_min, c.impressions_max, c.first_shown) == (10000, 15000, date(2025, 4, 21))


def test_parse_shown_countries_first_shown_falls_back_to_4():
    [c] = parse_shown_countries([{"1": 2250, "4": 20260219, "5": 20261122}])
    assert c.first_shown == date(2026, 2, 19)


def test_parse_shown_countries_unknown_geo():
    assert parse_shown_countries([{"1": 2999, "5": 20261122}])[0].country == "geo:2999"


def test_parse_shown_countries_skips_malformed():
    assert parse_shown_countries(None) == [] and parse_shown_countries({"1": 2840}) == []
    got = parse_shown_countries([{"5": 20261122}, {"1": 2840, "5": "soon"}, {"1": 2826, "5": 20261101}, "x"])
    assert [(c.country, c.last_shown) for c in got] == [("US", None), ("GB", date(2026, 11, 1))]


def test_client_detail_returns_raw_response_and_variations_still_parse():
    body = _load("creative_detail.json")
    client = TransparencyClient(FakeSession([body]))
    assert client.detail("AR1", "CR2") == body
    assert client.variations("AR1", "CR2") == parse_detail(body)
