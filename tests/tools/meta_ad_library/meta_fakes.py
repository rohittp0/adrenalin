"""A fake Meta (GraphQL pages by cursor, the detail fixture, fbcdn media) behind a real `PacedSession`."""
import json
from pathlib import Path
from urllib.parse import urlparse

import requests

from adrenalin.common.http import Cooldown, PacedSession
from adrenalin.tools.meta_ad_library.client import GRAPHQL_URL, SEARCH_DOC_ID
from adrenalin.tools.meta_ad_library.hosts import HOSTS, MEDIA_SUFFIX

FIX = Path(__file__).parents[2] / "fixtures" / "meta_ad_library"
PAGE_ID = "120740541030"


def load(name: str) -> str:
    return (FIX / name).read_text()


def page(name: str, next_cursor: str | None = None) -> str:
    """A fixture page with its `page_info` set: the last page when `next_cursor` is None."""
    data = json.loads(load(name))
    info = data["data"]["ad_library_main"]["search_results_connection"]["page_info"]
    info["has_next_page"], info["end_cursor"] = next_cursor is not None, next_cursor
    return json.dumps(data)


def response(body: str | bytes = b"", status=200, url=GRAPHQL_URL) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.url = url
    return r


class FakeMeta:
    def __init__(self, pages: dict[str | None, str], *, blocked: bool = False):
        self.pages, self.blocked = pages, blocked
        self.detail = load("detail_eu.json")
        self.requests: list[str] = []

    def __call__(self, method, url, data=None, headers=None, **kw):
        self.requests.append(url)
        if url == GRAPHQL_URL:
            if self.blocked:
                return response("", 302, url="https://www.facebook.com/login/")
            v = json.loads(data["variables"])
            if data["doc_id"] != SEARCH_DOC_ID:
                return response(self.detail)
            return response(self.pages[v["cursor"]])
        assert urlparse(url).hostname.endswith(MEDIA_SUFFIX), url
        return response(b"\xff\xd8JPEG", url=url)
