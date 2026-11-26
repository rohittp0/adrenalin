import pytest
import requests

from adrenalin.common.http import Blocked, Cooldown, PacedSession
from adrenalin.tools.ads_transparency.api import make_session
from adrenalin.tools.ads_transparency.hosts import GOOGLE_CHALLENGE, HOSTS, google_block
from adrenalin.tools.ads_transparency.rpc import DETAIL_URL, HOST, SEARCH_URL


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


@pytest.fixture
def rpc_session(tmp_path):
    now = 0.0
    replies = []

    def sleep(seconds):
        nonlocal now
        now += seconds

    def transport(method, url, **kwargs):
        return replies.pop(0) if replies else resp(url=url)

    session = PacedSession(Cooldown(tmp_path / "cooldown.sqlite3"), HOSTS,
                           transport=transport, clock=lambda: now, sleep=sleep, rng=lambda: 0.5)
    try:
        yield session, replies
    finally:
        session.close()


def test_rpc_search_and_country_detail_share_adaptive_pace(rpc_session):
    session, _ = rpc_session
    for _ in range(19):
        session.post(SEARCH_URL)
    assert session.interval(HOST) == 3.0
    session.post(DETAIL_URL)
    assert session.interval(HOST) == pytest.approx(2.7)
    for _ in range(400):
        session.post(DETAIL_URL)
    assert session.interval(HOST) == 1.2


@pytest.mark.parametrize("phase", [0.2, 1.5, 8.0])
def test_phase_pins_request_spacing_and_keeps_rate_limit_cooldown(tmp_path, phase):
    now = 0.0
    starts = []
    replies = []
    def sleep(seconds):
        nonlocal now
        now += seconds
    def transport(method, url, **kwargs):
        starts.append(now)
        return replies.pop(0) if replies else resp(url=url)
    session = make_session(tmp_path, phase=phase)
    session._clock = lambda: now
    session._sleep = sleep
    session._rng = lambda: 0.0
    session._transport = transport
    try:
        for _ in range(40):
            session.post(DETAIL_URL)
        assert [b - a for a, b in zip(starts, starts[1:])] == pytest.approx([phase] * 39)
        replies.append(resp(429, headers={"Retry-After": "120"}))
        session.post(SEARCH_URL)
        assert starts[-1] - starts[-2] >= 120
        assert session.interval(HOST) == phase
        assert session._hosts[HOST].challenge is GOOGLE_CHALLENGE
        assert session._hosts[HOST].browser
        assert HOSTS[HOST].pace.start == 3.0  # override must not mutate the defaults
    finally:
        session.close()


@pytest.mark.parametrize("failure", ["rate_limit", "captcha", "server_errors"])
def test_rpc_slows_after_acceleration(rpc_session, failure):
    session, replies = rpc_session
    for _ in range(20):
        session.post(DETAIL_URL)
    if failure == "rate_limit":
        replies.append(resp(429, headers={"Retry-After": "120"}))
        session.post(DETAIL_URL)
    elif failure == "captcha":
        replies.append(resp(302, headers={"Location": "https://www.google.com/sorry/index"}))
        with pytest.raises(Blocked):
            session.post(DETAIL_URL)
    else:
        for _ in range(3):
            replies.append(resp(503))
            session.post(DETAIL_URL)
    assert session.interval(HOST) == pytest.approx(5.4)


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
