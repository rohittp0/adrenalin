import json
from datetime import date
from pathlib import Path

import pytest
import requests

from adrenalin.common.http import Cooldown, PacedSession
from adrenalin.tools.meta_ad_library.client import (
    DETAIL_DOC_ID,
    GRAPHQL_URL,
    SEARCH_DOC_ID,
    MetaClient,
    MetaQuery,
    country_code,
)
from adrenalin.tools.meta_ad_library.hosts import HOSTS, LIBRARY_HOST, MEDIA_SUFFIX, meta_block
from adrenalin.tools.meta_ad_library.parse import DocNotFound, EuReach, GraphQLError

FIX = Path(__file__).parents[2] / "fixtures" / "meta_ad_library"
PAGE_ID = "120740541030"


def load(name: str) -> str:
    return (FIX / name).read_text()


def response(body: str | bytes = b"", status=200, headers=None, url=GRAPHQL_URL) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.url = url
    r.headers.update(headers or {})
    return r


class Unread(requests.Response):
    """A streamed response: reading its body is a bug."""

    @property
    def content(self):
        raise AssertionError("body read on a streamed response")


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeGraphQL:
    """Captures POST forms and answers with one body (or a callable of the decoded variables)."""

    def __init__(self, reply):
        self.reply = reply
        self.forms: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, method, url, data=None, headers=None, **kw):
        assert (method, url) == ("POST", GRAPHQL_URL)
        self.forms.append(data)
        self.headers.append(headers or {})
        body = self.reply(json.loads(data["variables"])) if callable(self.reply) else self.reply
        return response(body)


@pytest.fixture
def client_for(tmp_path):
    sessions = []

    def make(reply, today=date(2026, 11, 26)):
        fake = FakeGraphQL(reply)
        clock = Clock()
        session = PacedSession(Cooldown(tmp_path / "cooldown.sqlite3", clock=clock), HOSTS, transport=fake,
                               sleep=lambda s: None, clock=clock)
        sessions.append(session)
        return MetaClient(session, today=lambda: today), fake

    yield make
    for s in sessions:
        s.close()


def query(country="IN", status="all", start=date(2026, 11, 1), end=date(2026, 11, 20)) -> MetaQuery:
    return MetaQuery(PAGE_ID, country, status, start, end)


# -- host rules -----------------------------------------------------------------


def test_meta_block_rules():
    login = response(status=302, headers={"Location": "https://www.facebook.com/login/?next=x"})
    checkpoint = response(status=302, headers={"Location": "https://www.facebook.com/checkpoint/1501092823525282/"})
    assert meta_block(login, True) and meta_block(checkpoint, True)
    assert meta_block(response("<html>", url="https://www.facebook.com/checkpoint/block/?next"), True)
    assert meta_block(response("<html>", url="https://www.facebook.com/login.php"), True)
    assert meta_block(response("<!DOCTYPE html><html>log in</html>"), True)
    assert meta_block(response(b""), True)  # an empty GraphQL answer isn't JSON either
    rate = {"errors": [{"message": "Rate limit exceeded", "code": 1675004}]}
    assert meta_block(response(json.dumps(rate)), True)
    assert meta_block(response(json.dumps({"errors": [{"message": "Please slow down: RATE LIMITED"}]})), True)
    assert meta_block(response(json.dumps({"errors": [{"message": "x", "code": 1675004}]})), True)
    # a normal page, and GraphQL errors that are not rate limits (an expired cursor, a changed doc_id)
    assert meta_block(response(load("search_page.json")), True) is None
    assert meta_block(response(load("doc_not_found.json")), True) is None
    assert meta_block(response(json.dumps({"errors": [{"message": "bad cursor", "code": 1}]})), True) is None
    # media and other pages are not GraphQL answers: their bodies are never judged
    assert meta_block(response(b"\xff\xd8 jpeg", url="https://scontent-maa5-1.xx.fbcdn.net/v/a.jpg?x=1"), True) is None


def test_meta_block_never_reads_a_streamed_body():
    r = Unread()
    r.status_code, r.url = 200, GRAPHQL_URL
    assert meta_block(r, False) is None
    r.headers["Location"] = "https://www.facebook.com/login/"
    assert meta_block(r, False)


def test_hosts():
    assert set(HOSTS) == {LIBRARY_HOST, MEDIA_SUFFIX}
    library = HOSTS[LIBRARY_HOST]
    assert library.browser and library.blocked is meta_block and library.challenge is None
    assert not HOSTS[MEDIA_SUFFIX].browser


# -- client ---------------------------------------------------------------------


def test_search_form(client_for):
    client, fake = client_for(load("search_page.json"))
    page = client.search(query(), None)
    assert len(page.creatives) == 10 and page.next_cursor
    form = fake.forms[0]
    assert form["doc_id"] == SEARCH_DOC_ID
    assert form["fb_api_req_friendly_name"] == "AdLibrarySearchPaginationQuery"
    assert fake.headers[0]["x-fb-friendly-name"] == "AdLibrarySearchPaginationQuery"
    assert form["lsd"] == "" and form["__a"] == "1" and form["av"] == "0"
    v = json.loads(form["variables"])
    assert v["viewAllPageID"] == PAGE_ID
    assert v["countries"] == ["IN"]
    assert v["activeStatus"] == "all"
    assert v["startDate"] == {"min": "2026-11-01", "max": "2026-11-20"}
    assert v["cursor"] is None and v["first"] == 10 and v["searchType"] == "page" and v["adType"] == "ALL"

    client.search(MetaQuery(PAGE_ID, country_code("anywhere"), "active", None, None), "CUR")
    v = json.loads(fake.forms[1]["variables"])
    assert v["countries"] == ["ALL"] and v["cursor"] == "CUR" and v["activeStatus"] == "active"
    assert v["startDate"] is None


def test_open_ended_shown_period(client_for):
    client, fake = client_for(load("search_page.json"), today=date(2026, 11, 26))
    client.search(query(start=None, end=date(2026, 3, 1)), None)
    client.search(query(start=date(2026, 3, 1), end=None), None)
    dates = [json.loads(f["variables"])["startDate"] for f in fake.forms]
    assert dates == [{"min": "2018-01-01", "max": "2026-03-01"}, {"min": "2026-03-01", "max": "2026-11-26"}]


def test_country_code():
    assert country_code("anywhere") == "ALL"
    assert country_code("in") == "IN"


def test_detail(client_for):
    client, fake = client_for(load("detail_eu.json"))
    reach = client.detail(query(), "1535930091652737")
    assert isinstance(reach, EuReach) and reach.eu_total == 22
    form = fake.forms[0]
    assert form["doc_id"] == DETAIL_DOC_ID
    v = json.loads(form["variables"])
    assert v["adArchiveID"] == "1535930091652737" and v["pageID"] == PAGE_ID and v["country"] == "ALL"
    assert v["isAdNonPolitical"] is True and v["isAdNotAAAEligible"] is False


def test_doc_not_found_is_raised(client_for):
    client, _ = client_for(load("doc_not_found.json"))
    with pytest.raises(DocNotFound):
        client.search(query(), None)
    with pytest.raises(DocNotFound):
        client.detail(query(), "1")


@pytest.mark.parametrize("body", [
    '{"data": {}}',  # KeyError in the parser
    '{"data": {"ad_library_main": null}}',  # TypeError
    '{"errors": ["not an object"]}',  # AttributeError in check_errors
    '{"data": {"ad_library_main": {"search_results_connection": ',  # JSONDecodeError
])
def test_broken_response_shape_is_a_graphql_error(client_for, body):
    client, _ = client_for(body)
    with pytest.raises(GraphQLError, match="unexpected response shape") as e:
        client.search(query(), None)
    assert not isinstance(e.value, DocNotFound)
    with pytest.raises(GraphQLError, match="unexpected response shape"):
        client.detail(query(), "1")
