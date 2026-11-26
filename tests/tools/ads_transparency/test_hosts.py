import pytest
import requests

from adrenalin.common.http import Blocked
from adrenalin.tools.ads_transparency.api import make_session
from adrenalin.tools.ads_transparency.hosts import GOOGLE_CHALLENGE, HOSTS, google_block
from adrenalin.tools.ads_transparency.rpc import HOST


def resp(status=200, body=b"{}", headers=None, url="https://a.com/x", ctype=None):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.url = url
    r.headers.update(headers or {})
    if ctype:
        r.headers["Content-Type"] = ctype
    return r


def test_google_block_rules():
    assert google_block(resp(302, headers={"Location": "https://www.google.com/sorry/index"}), True) == "/sorry/ redirect"
    r = resp(200, b"<html>please solve recaptcha</html>", ctype="text/html; charset=utf-8")
    assert google_block(r, True) == "reCAPTCHA page"
    assert google_block(r, False) is None
    assert google_block(resp(200, b"recaptcha", ctype="application/json"), True) is None


def test_transparency_host_uses_the_browser_and_the_google_challenge():
    assert HOSTS[HOST].browser and HOSTS[HOST].challenge is GOOGLE_CHALLENGE
    assert GOOGLE_CHALLENGE.shown("https://www.google.com/sorry/index")


def test_oembed_still_detects_blocks(tmp_path):
    def transport(method, url, **kwargs):
        return resp(302, headers={"Location": "https://www.google.com/sorry/index"}, url=url)

    session = make_session(tmp_path)
    session._transport = transport
    try:
        with pytest.raises(Blocked):
            session.get("https://www.youtube.com/oembed?url=x")
    finally:
        session.close()
