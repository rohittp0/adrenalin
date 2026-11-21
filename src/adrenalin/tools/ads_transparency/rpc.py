"""Google Ads Transparency internal RPC client and response parsers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from adrenalin.common.http import PacedSession

HOST = "adstransparency.google.com"
SEARCH_URL = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives"
DETAIL_URL = "https://adstransparency.google.com/anji/_/rpc/LookupService/GetCreativeById"

_FORMATS = {1: "text", 2: "image", 3: "video"}
_IGNORED_JS_PARAMS = {"htmlParentId", "responseCallback"}
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
# Geo ID of the viewer context the web UI sends with every request (2356 = India, where these requests were
# captured). It is not the Region filter (search filter "8"), which comes from `Query.geo_id`.
VIEWER_GEO_ID = 2356


class PaginationLoop(ValueError):
    pass


class RpcError(ValueError):
    """A Transparency RPC answered with a non-200 status."""


@dataclass(frozen=True)
class Query:
    advertiser_id: str
    geo_id: int | None
    start: date | None
    end: date | None


@dataclass(frozen=True)
class Preview:
    kind: Literal["image", "content_js"]
    url: str
    width: int | None
    height: int | None

    @property
    def image_id(self) -> str | None:
        if "simgad" not in self.url:
            return None
        return urlparse(self.url).path.rstrip("/").rsplit("/", 1)[-1] or None


@dataclass(frozen=True)
class ListedCreative:
    creative_id: str
    advertiser_name: str
    format: Literal["text", "image", "video", "unknown"]
    first_shown: datetime
    last_shown: datetime
    days_shown: int | None
    preview: Preview | None


@dataclass(frozen=True)
class SearchPage:
    creatives: list[ListedCreative]
    next_cursor: str | None
    count_low: int | None
    count_high: int | None
    skipped: int


def _yyyymmdd(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def build_search_request(query: Query, cursor: str | None, page_size: int = 100) -> dict:
    filters: dict[str, Any] = {"12": {"1": "", "2": True}, "13": {"1": [query.advertiser_id]}}
    if query.geo_id:
        filters["8"] = [query.geo_id]
    if query.start:
        filters["6"] = _yyyymmdd(query.start)
    if query.end:
        # The request's end date is exclusive.
        filters["7"] = _yyyymmdd(query.end + timedelta(days=1))
    req: dict[str, Any] = {"2": page_size, "3": filters, "7": {"1": 1, "2": 0, "3": VIEWER_GEO_ID}}
    if cursor:
        req["4"] = cursor
    return req


def _build_detail_request(advertiser_id: str, creative_id: str) -> dict:
    return {"1": advertiser_id, "2": creative_id, "5": {"1": 1, "2": 0, "3": VIEWER_GEO_ID}}


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""\b{name}\s*=\s*["']([^"']*)["']""", tag, re.I)
    return m.group(1) if m else None


def parse_preview(obj: dict) -> Preview | None:
    if not isinstance(obj, dict):
        return None
    inner = obj.get("3")
    if not isinstance(inner, dict):
        return None
    # Search results nest the HTML at "3"."3"."2"; detail variations at "3"."2".
    html = inner.get("2")
    if not isinstance(html, str) and isinstance(inner.get("3"), dict):
        html = inner["3"].get("2")
    if isinstance(html, str):
        m = _IMG_RE.search(html)
        if m:
            src = _attr(m.group(0), "src")
            if src:
                return Preview(
                    "image",
                    src,
                    _int_or_none(_attr(m.group(0), "width")),
                    _int_or_none(_attr(m.group(0), "height")),
                )
    js = (inner.get("1") or {}).get("4") if isinstance(inner.get("1"), dict) else None
    if isinstance(js, str) and js:
        return Preview("content_js", js, None, None)
    return None


def _ts(v: dict) -> datetime:
    return datetime.fromtimestamp(int(v["1"]) + int(v.get("2", 0)) / 1e9, tz=timezone.utc)


def _parse_creative(c: dict) -> ListedCreative:
    if not isinstance(c["2"], str) or not c["2"]:
        raise ValueError("creative id must be a non-empty string")
    return ListedCreative(
        creative_id=c["2"],
        advertiser_name=c.get("12") or "",
        format=_FORMATS.get(_int_or_none(c.get("4")), "unknown"),
        first_shown=_ts(c["6"]),
        last_shown=_ts(c["7"]),
        days_shown=_int_or_none(c.get("13")),
        preview=parse_preview(c),
    )


def parse_search_page(data: dict) -> SearchPage:
    items = data.get("1", [])
    if not isinstance(items, list):
        raise ValueError("search response has no creatives list")
    creatives: list[ListedCreative] = []
    skipped = 0
    for c in items:
        try:
            creatives.append(_parse_creative(c))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
            skipped += 1
    cursor = data.get("2")
    return SearchPage(
        creatives,
        cursor if isinstance(cursor, str) and cursor else None,
        _int_or_none(data.get("4")),
        _int_or_none(data.get("5")),
        skipped,
    )


def _preview_key(p: Preview) -> tuple:
    if p.image_id:
        return ("img", p.image_id)
    u = urlparse(p.url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in _IGNORED_JS_PARAMS]
    return ("js", urlunparse(u._replace(query=urlencode(q))))


def parse_detail(data: dict) -> list[Preview]:
    inner = data.get("1")
    if not isinstance(inner, dict) or not isinstance(inner.get("5"), list):
        raise ValueError("detail response has no variations list")
    raw = inner["5"]
    out: list[Preview] = []
    seen: set = set()
    for obj in raw:
        p = parse_preview(obj)
        if p is None:
            continue
        k = _preview_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def transparency_url(advertiser_id: str, creative_id: str, region: str) -> str:
    return f"https://{HOST}/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"


def preview_ids(url: str) -> tuple[str, str]:
    """(Ads account ID, Ad group ID) a content.js Preview URL exposes; `""` for each that is absent."""
    q = dict(parse_qsl(urlparse(url).query))
    return q.get("obfuscatedCustomerId", ""), q.get("adGroupId", "")


class TransparencyClient:
    def __init__(self, session: PacedSession):
        self._s = session

    def _post(self, url: str, body: dict, advertiser_id: str) -> dict:
        resp = self._s.post(
            url,
            params={"authuser": "0"},
            data={"f.req": json.dumps(body, separators=(",", ":"))},
            headers={
                "X-Same-Domain": "1",
                "Origin": f"https://{HOST}",
                "Referer": f"https://{HOST}/advertiser/{advertiser_id}",
            },
        )
        if resp.status_code != 200:
            # A JSON error body would otherwise parse as an empty last page and truncate the listing.
            raise RpcError(f"HTTP {resp.status_code} from {url}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ValueError(f"non-JSON response from {url}") from e
        if not isinstance(data, dict):
            raise ValueError(f"unexpected response shape from {url}")
        return data

    def search(self, query: Query, cursor: str | None = None, page_size: int = 100) -> SearchPage:
        data = self._post(SEARCH_URL, build_search_request(query, cursor, page_size), query.advertiser_id)
        return parse_search_page(data)

    def iter_pages(self, query: Query, start_cursor: str | None = None) -> Iterator[SearchPage]:
        cursor = start_cursor
        seen: set[str] = set()
        if cursor:
            seen.add(cursor)
        while True:
            page = self.search(query, cursor)
            yield page
            nxt = page.next_cursor
            if not nxt:
                return
            if nxt in seen:
                raise PaginationLoop(f"cursor repeated: {nxt}")
            seen.add(nxt)
            cursor = nxt

    def variations(self, advertiser_id: str, creative_id: str) -> list[Preview]:
        data = self._post(DETAIL_URL, _build_detail_request(advertiser_id, creative_id), advertiser_id)
        return parse_detail(data)

    def advertiser_name(self, query: Query) -> str | None:
        unfiltered = Query(query.advertiser_id, None, None, None)
        for q in (query,) if query == unfiltered else (query, unfiltered):
            for c in self.search(q, page_size=1).creatives:
                if c.advertiser_name:
                    return c.advertiser_name
        return None
