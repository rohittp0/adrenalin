"""Per-host policies of the meta-ad-library tool: pacing, browser use and Meta's block rules (ADR 0004)."""
from __future__ import annotations

import json

import requests

from adrenalin.common.http import AdaptivePace, HostPolicy

LIBRARY_HOST = "www.facebook.com"
MEDIA_SUFFIX = ".fbcdn.net"  # every scontent-*.fbcdn.net media host shares one pace and cooldown
GRAPHQL_PATH = "/api/graphql/"
_LOGGED_OUT_WALLS = ("/login", "/checkpoint")
# Unverified (ADR 0004): no run has been refused under load yet, so Meta's rate-limit answer is a guess.
_RATE_LIMIT_CODES = {1675004}


def _rate_limited(content: bytes) -> bool:
    if b'"errors"' not in content:
        return False  # a normal page: don't parse a few hundred KB twice
    try:
        errors = json.loads(content).get("errors")
    except (ValueError, AttributeError):
        return False  # unparsable: the client reports its shape
    for error in errors if isinstance(errors, list) else ():
        if not isinstance(error, dict):
            continue
        if error.get("code") in _RATE_LIMIT_CODES or "rate limit" in str(error.get("message", "")).lower():
            return True
    return False


def meta_block(resp: requests.Response, read_body: bool) -> str | None:
    """Why a response is Meta refusing a logged-out client, else None (ADR 0004). A redirect to (or a page at)
    /login or /checkpoint is a block, never a challenge to solve: solving it means logging in. So is any HTTP error
    status but 429 (which backs off and retries like on every host), whether on the origin page the browser opens
    or on a GraphQL request: Meta answers queries with HTTP 200, errors included. A GraphQL answer that isn't a JSON
    object is a block too, as is a rate-limit error."""
    location = resp.headers.get("Location", "")
    for wall in _LOGGED_OUT_WALLS:
        if wall in location:
            return f"redirect to {wall}"
        if wall in (resp.url or ""):
            return f"{wall} page"
    if resp.status_code == 429:
        return None
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}"
    if not read_body or GRAPHQL_PATH not in (resp.url or ""):
        return None
    content = resp.content or b""
    if not content.lstrip().startswith(b"{"):
        return "GraphQL answer is not JSON"
    if _rate_limited(content):
        return "GraphQL rate-limit error"
    return None


HOSTS: dict[str, HostPolicy] = {
    # /login and /checkpoint are blocks, never a challenge to solve (ADR 0004)
    LIBRARY_HOST: HostPolicy(AdaptivePace(4.0, 2.5, 30.0, clean_streak=30), browser=True, blocked=meta_block,
                             challenge=None),
    MEDIA_SUFFIX: HostPolicy(AdaptivePace(1.0, 0.3, 8.0)),
}
