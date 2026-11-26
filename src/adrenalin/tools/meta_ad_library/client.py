"""Meta Ad Library client: the web Ad Library's internal GraphQL queries (ADR 0004).

No HTML request and no token: every listing page, page 1 included, is one `AdLibrarySearchPaginationQuery`, and
a Creative's EU reach is one `AdLibraryV3AdDetailsQuery`. The `doc_id`s come from Meta's JS bundles and change
when Meta deploys; a changed one answers "GraphQL document ... not found" (`DocNotFound`).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal, TypeVar

import requests

from adrenalin.common.http import PacedSession
from adrenalin.tools.meta_ad_library.hosts import GRAPHQL_PATH, LIBRARY_HOST
from adrenalin.tools.meta_ad_library.parse import (
    EuReach,
    GraphQLError,
    SearchPage,
    check_errors,
    parse_detail,
    parse_search_page,
)

GRAPHQL_URL = f"https://{LIBRARY_HOST}{GRAPHQL_PATH}"
SEARCH_DOC_ID = "29218106027816007"
DETAIL_DOC_ID = "25068828942793558"
RELAY_V = "682466"
SEARCH_QUERY = "AdLibrarySearchPaginationQuery"
DETAIL_QUERY = "AdLibraryV3AdDetailsQuery"
# The open start of a Shown period: no Page's ads in the Ad Library go back further.
EARLIEST_START = date(2018, 1, 1)
PAGE_SIZE = 10

T = TypeVar("T")


@dataclass(frozen=True)
class MetaQuery:
    """One Page x one Region x one Shown period (and the Creatives' running status)."""
    page_id: str
    country: str  # ISO code, or "ALL" for the Region `anywhere` (see `country_code`)
    status: Literal["all", "active", "inactive"]
    start: date | None
    end: date | None


def country_code(region: str) -> str:
    """Meta's country filter for a Region: the ISO code, or "ALL" for `anywhere`."""
    return "ALL" if region.lower() == "anywhere" else region.upper()


class MetaClient:
    def __init__(self, session: PacedSession, *, today: Callable[[], date] = date.today):
        self._s = session
        self._today = today

    def _post(self, query_name: str, doc_id: str, variables: dict) -> str:
        form = {
            "av": "0", "__user": "0", "__a": "1",
            "lsd": "",  # not checked for logged-out requests (ADR 0004)
            "fb_api_caller_class": "RelayModern", "fb_api_req_friendly_name": query_name,
            "server_timestamps": "true", "doc_id": doc_id,
            "variables": json.dumps(variables, separators=(",", ":")),
        }
        # Read-only queries: a retry resends the same cursor and passes through pacing and cooldown again.
        for attempt in range(3):
            try:
                resp = self._s.post(GRAPHQL_URL, data=form, headers={"x-fb-friendly-name": query_name})
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                if isinstance(exc, requests.exceptions.SSLError) or attempt == 2:
                    raise
        # Errors come as HTTP 200 with an `errors` array, so the body decides, never the status (ADR 0004).
        return resp.content.decode("utf-8", errors="replace")

    @staticmethod
    def _parse(parser: Callable[[str], T], body: str) -> T:
        """`check_errors`, then the parser. A structurally broken answer becomes a `GraphQLError`, so it fails
        only its queue item; `DocNotFound` and other GraphQL errors pass through unchanged."""
        try:
            check_errors(body)
            return parser(body)
        except GraphQLError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError) as exc:  # ValueError covers JSONDecodeError
            raise GraphQLError(f"unexpected response shape: {type(exc).__name__}: {exc}") from exc

    def _shown_period(self, query: MetaQuery) -> dict | None:
        if query.start is None and query.end is None:
            return None
        start = query.start or EARLIEST_START
        end = query.end or self._today()
        return {"min": start.isoformat(), "max": end.isoformat()}

    def search(self, query: MetaQuery, cursor: str | None) -> SearchPage:
        """One listing page of the Page's Creatives; `cursor=None` is page 1."""
        variables = {
            "activeStatus": query.status, "adType": "ALL", "bylines": [], "collationToken": None,
            "contentLanguages": [], "countries": [query.country], "cursor": cursor or None, "excludedIDs": None,
            "first": PAGE_SIZE, "isTargetedCountry": False, "location": None, "mediaType": "all",
            "multiCountryFilterMode": None, "pageIDs": [], "potentialReachInput": None, "publisherPlatforms": [],
            "queryString": "", "regions": None, "searchType": "page", "sessionID": None, "sortData": None,
            "source": None, "startDate": self._shown_period(query), "v": RELAY_V, "viewAllPageID": query.page_id,
        }
        return self._parse(parse_search_page, self._post(SEARCH_QUERY, SEARCH_DOC_ID, variables))

    def detail(self, query: MetaQuery, library_id: str) -> EuReach | None:
        """A Creative's EU reach; None when Meta publishes none for it."""
        variables = {
            "adArchiveID": library_id, "pageID": query.page_id, "country": "ALL", "isAdNonPolitical": True,
            "isAdNotAAAEligible": False, "sessionID": None, "source": None,
        }
        return self._parse(parse_detail, self._post(DETAIL_QUERY, DETAIL_DOC_ID, variables))
