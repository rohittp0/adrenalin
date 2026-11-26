"""Per-host policies of the ads-transparency tool: pacing, browser use and Google's block rules."""
from __future__ import annotations

import requests

from adrenalin.common.http import AdaptivePace, Challenge, HostPolicy
from adrenalin.tools.ads_transparency.rpc import HOST

RPC_INTERVAL = (6.0, 2.0)
# content.js previously blocked at 0.5 s: accelerate cautiously and back off on errors.
SCRIPT_HOST = "displayads-formats.googleusercontent.com"
SCRIPT_PACE = AdaptivePace(2.5, 1.5, 15.0, clean_streak=30)
# simgad images: never blocked so far. Paced adaptively: from one a second, faster while responses stay clean,
# slower on errors (see `AdaptivePace`).
IMAGE_HOST, IMAGE_PACE = "tpc.googlesyndication.com", AdaptivePace(1.0, 0.2, 4.0)


def google_block(resp: requests.Response, read_body: bool) -> str | None:
    """Why a response is Google's block page (the `/sorry/` redirect or a reCAPTCHA page), else None."""
    if "/sorry/" in (resp.url or "") or "/sorry/" in resp.headers.get("Location", ""):
        return "/sorry/ redirect"
    if read_body and "text/html" in resp.headers.get("Content-Type", "").lower():
        text = resp.content[:4000].decode("utf-8", errors="ignore").lower()
        if "recaptcha" in text:
            return "reCAPTCHA page"
    return None


GOOGLE_CHALLENGE = Challenge(
    shown=lambda url: "/sorry/" in url,
    keep_cookies=frozenset({"GOOGLE_ABUSE_EXEMPTION"}),
    cookie_domains=frozenset({"google.com", "adstransparency.google.com"}),
)

HOSTS: dict[str, HostPolicy] = {
    HOST: HostPolicy(RPC_INTERVAL, browser=True, blocked=google_block, challenge=GOOGLE_CHALLENGE),
    SCRIPT_HOST: HostPolicy(SCRIPT_PACE, blocked=google_block),
    IMAGE_HOST: HostPolicy(IMAGE_PACE, blocked=google_block),
}
# Hosts the tool does not declare (YouTube oEmbed, the OCR domain check's arbitrary hosts) keep Google's rules.
DEFAULT = HostPolicy(blocked=google_block)
