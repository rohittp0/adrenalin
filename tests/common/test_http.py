import requests

import pytest

from adrenalin.common.http import Blocked, Cooldown, PacedSession, is_block_response

SORRY = "https://www.google.com/sorry/index"


def resp(status=200, body=b"{}", headers=None, url="https://a.com/x", ctype=None):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.url = url
    r.headers.update(headers or {})
    if ctype:
        r.headers["Content-Type"] = ctype
    return r


class Env:
    def __init__(self, tmp_path, replies=(), **kw):
        self.now = 0.0
        self.sleeps = []
        self.calls = []
        self.replies = list(replies)
        self.cooldown = Cooldown(tmp_path / "cd.sqlite")
        intervals = kw.pop("intervals", {})
        self.session = PacedSession(
            self.cooldown, intervals, transport=self.transport,
            sleep=self.sleep, clock=lambda: self.now, rng=kw.pop("rng", lambda: 0.5), **kw,
        )

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def transport(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.replies.pop(0) if self.replies else resp()


def test_spaces_requests_per_host_independently(tmp_path):
    e = Env(tmp_path, intervals={"a.com": (6, 0)})
    e.session.get("https://a.com/1")
    e.session.get("https://a.com/2")
    e.session.get("https://b.com/1")
    assert e.sleeps == [6]


@pytest.mark.parametrize("r,delay", [(0.0, 4), (1.0, 8)])
def test_jitter_within_bounds(tmp_path, r, delay):
    e = Env(tmp_path, intervals={"a.com": (6, 2)}, rng=lambda: r)
    e.session.get("https://a.com/1")
    e.session.get("https://a.com/2")
    assert e.sleeps == [delay]


def test_429_backs_off_with_retry_after_then_succeeds(tmp_path):
    e = Env(tmp_path, [resp(429, headers={"Retry-After": "120"}), resp(200)])
    assert e.session.get("https://a.com/1").status_code == 200
    assert 120 in e.sleeps


def test_429_exhausting_retries_raises_blocked_and_persists(tmp_path):
    e = Env(tmp_path, [resp(429)] * 4, max_retries=3)
    with pytest.raises(Blocked):
        e.session.get("https://a.com/1")
    assert len(e.calls) == 4
    assert Cooldown(tmp_path / "cd.sqlite").remaining("a.com", now=e.now) > 0


def test_sorry_redirect_blocks_for_900s_without_retry(tmp_path):
    e = Env(tmp_path, [resp(302, headers={"Location": SORRY})])
    e.now = 1000.0
    with pytest.raises(Blocked) as ei:
        e.session.get("https://www.google.com/x")
    assert ei.value.until == 1900.0
    assert len(e.calls) == 1


def test_recaptcha_html_counts_as_block(tmp_path):
    r = resp(200, b"<html>please solve recaptcha</html>", ctype="text/html; charset=utf-8")
    assert is_block_response(r)
    assert not is_block_response(resp(200, b"recaptcha", ctype="application/json"))
    e = Env(tmp_path, [r])
    with pytest.raises(Blocked):
        e.session.get("https://a.com/1")


def test_active_cooldown_raises_before_sending(tmp_path):
    e = Env(tmp_path)
    e.cooldown.block("a.com", 60, now=0.0)
    with pytest.raises(Blocked):
        e.session.get("https://a.com/1")
    assert e.calls == []


def test_wait_on_block_sleeps_through_cooldown_and_retries(tmp_path):
    e = Env(tmp_path, [resp(302, headers={"Location": SORRY}), resp(200)], wait_on_block=True)
    assert e.session.get("https://a.com/1").status_code == 200
    assert sum(e.sleeps) >= 900


def test_proxies_rotate_per_request(tmp_path):
    e = Env(tmp_path, proxies=["p1", "p2"])
    for _ in range(3):
        e.session.get("https://a.com/1")
    assert [c[2]["proxies"]["https"] for c in e.calls] == ["p1", "p2", "p1"]
    assert all(c[2]["allow_redirects"] is False for c in e.calls)


def test_wait_on_block_rechecks_cooldown_extended_during_sleep(tmp_path):
    e = Env(tmp_path, [resp(200)], wait_on_block=True)
    e.cooldown.block("a.com", 60, now=0.0)
    orig = e.sleep

    def extending_sleep(s):
        orig(s)
        if len(e.sleeps) == 1:  # another thread extends the cooldown meanwhile
            e.cooldown.block("a.com", 100, now=e.now)

    e.session._sleep = extending_sleep
    assert e.session.get("https://a.com/1").status_code == 200
    assert e.sleeps == [60, 100]


def test_derive_shares_cooldown_and_transport_but_fails_fast(tmp_path):
    e = Env(tmp_path, [resp(429), resp(302, headers={"Location": SORRY})], wait_on_block=True,
            proxies=["p1"])
    fast = e.session.derive(wait_on_block=False, max_retries=0)
    with pytest.raises(Blocked):  # 429: no backoff sleep, no retry
        fast.get("https://a.com/1")
    assert len(e.calls) == 1 and e.sleeps == []
    assert e.cooldown.remaining("a.com", now=e.now) > 0  # shared cooldown
    with pytest.raises(Blocked):  # cooldown active: raises instead of waiting
        fast.get("https://a.com/2")
    assert len(e.calls) == 1 and e.sleeps == []
    with pytest.raises(Blocked):  # sorry on another host: no wait, no retry
        fast.get("https://b.com/1")
    assert len(e.calls) == 2 and e.sleeps == []
    assert e.calls[0][2]["proxies"]["https"] == "p1"
    fast.close()


def _checker_env(tmp_path, outcomes):
    """A HostChecker whose transport answers per host: an int status, or an exception to raise."""
    from adrenalin.common.cache import ExtractionCache
    from adrenalin.common.http import HostChecker

    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        out = outcomes[url.split("/")[2]]
        if isinstance(out, BaseException):
            raise out
        return out if isinstance(out, requests.Response) else resp(out, url=url)

    cache = ExtractionCache(tmp_path / "x.sqlite3")
    session = PacedSession(Cooldown(tmp_path / "cd.sqlite"), {}, transport=transport, sleep=lambda s: None,
                           max_retries=0)
    return HostChecker(session, cache), cache, session, calls


def test_host_checker_any_response_means_the_host_exists(tmp_path):
    from adrenalin.common.http import HostChecker

    outcomes = {"flights70.com": 200, "forbidden.com": 403, "broken.com": 503,
                "flights7o.com": _dns_error("flights7o.com"), "www.flights7o.com": _dns_error("www.flights7o.com")}
    check, cache, session, calls = _checker_env(tmp_path, outcomes)
    try:
        assert [check(h) for h in list(outcomes)[:4]] == [True, True, True, False]
        method, url, kw = calls[0]
        assert (method, url) == ("GET", "https://flights70.com/")
        assert kw["timeout"] == 5 and kw["stream"] is True and kw["allow_redirects"] is True
        # one request per name per run, whatever the verdict
        assert check("FLIGHTS70.com") and not check("flights7o.com") and len(calls) == len(outcomes)
        # positive verdicts persist across runs; negative ones are re-checked (a network outage is temporary)
        again = HostChecker(session, cache)
        assert again("flights70.com") and not again("flights7o.com")
        assert [u for _, u, _ in calls[len(outcomes):]] == ["https://flights7o.com/", "https://www.flights7o.com/"]
    finally:
        cache.close()


def test_host_checker_counts_a_block_page_as_existing(tmp_path):
    check, cache, _, calls = _checker_env(tmp_path, {"google.com": resp(302, headers={"Location": SORRY})})
    try:
        assert check("google.com") and len(calls) == 1
    finally:
        cache.close()


def test_streamed_request_never_reads_the_body(tmp_path):
    class Unread(requests.Response):
        @property
        def content(self):
            raise AssertionError("body read")

    r = Unread()
    r.status_code, r.url = 200, "https://example.com/"
    r.headers["Content-Type"] = "text/html"
    e = Env(tmp_path, [r, resp(302, headers={"Location": SORRY})])
    assert e.session.get("https://example.com/", stream=True) is r
    with pytest.raises(Blocked):  # a /sorry/ redirect is still a block
        e.session.get("https://example.com/", stream=True)


def _dns_error(host):
    """The exception chain requests raises when a name does not resolve."""
    import socket

    from urllib3.exceptions import MaxRetryError, NameResolutionError

    reason = NameResolutionError(host, None, socket.gaierror(8, "nodename nor servname provided, or not known"))
    return requests.ConnectionError(MaxRetryError(None, "/", reason))


def test_host_checker_only_dns_failure_means_missing(tmp_path):
    from adrenalin.common.http import HostChecker

    outcomes = {"flights7o.com": _dns_error("flights7o.com"), "www.flights7o.com": _dns_error("www.flights7o.com"),
                "slow.com": requests.Timeout("timed out"), "badtls.com": requests.exceptions.SSLError("bad cert"),
                "refused.com": requests.ConnectionError("[Errno 61] Connection refused")}
    check, cache, session, calls = _checker_env(tmp_path, outcomes)
    try:
        assert not check("flights7o.com")
        assert check("slow.com") and check("badtls.com") and check("refused.com")
        # inconclusive errors are not remembered across runs; DNS failures neither
        again = HostChecker(session, cache)
        n = len(calls)
        assert again("slow.com") and not again("flights7o.com") and len(calls) > n
    finally:
        cache.close()


def test_host_checker_tries_the_other_www_form_and_logs_misses_once(tmp_path):
    from adrenalin.common.http import HostChecker

    outcomes = {"www.bare-only.com": _dns_error("www.bare-only.com"), "bare-only.com": 200,
                "www-only.com": _dns_error("www-only.com"), "www.www-only.com": 200,
                "www.gone.com": _dns_error("www.gone.com"), "gone.com": _dns_error("gone.com")}
    check, cache, session, calls = _checker_env(tmp_path, outcomes)
    logs = []
    check = HostChecker(session, cache, log=logs.append)
    try:
        assert check("www.bare-only.com") and check("www-only.com")
        assert not check("www.gone.com") and not check("gone.com") and not check("www.gone.com")
        assert len(logs) == 1 and "gone.com" in logs[0]
        assert [u for _, u, _ in calls].count("https://gone.com/") == 1  # each name requested once per run
    finally:
        cache.close()


def test_block_logs_what_the_response_looked_like(tmp_path):
    logs = []
    page = resp(200, b"<html>please solve the recaptcha</html>", url="https://cdn.com/x.js", ctype="text/html")
    e = Env(tmp_path, [page], log=logs.append)
    with pytest.raises(Blocked) as exc:
        e.session.get("https://cdn.com/x.js")
    assert len(logs) == 1 and logs[0].startswith("warning: cdn.com sent a block page")
    assert "HTTP 200" in logs[0] and "text/html" in logs[0] and "recaptcha" in logs[0]
    assert "cdn.com blocked until" in str(exc.value)


def test_429_exhaustion_logs_the_last_response(tmp_path):
    logs = []
    e = Env(tmp_path, [resp(429)] * 4, log=logs.append)
    with pytest.raises(Blocked):
        e.session.get("https://a.com/x")
    assert len(logs) == 1 and "rate-limits after 3 backoffs" in logs[0] and "HTTP 429" in logs[0]


def test_derived_session_keeps_the_log(tmp_path):
    logs = []
    e = Env(tmp_path, [resp(302, headers={"Location": SORRY})], log=logs.append)
    with pytest.raises(Blocked):
        e.session.derive(max_retries=0).get("https://a.com/x")
    assert "Location=" + SORRY in logs[0]


from adrenalin.common.http import AdaptivePace  # noqa: E402


def _adaptive_env(tmp_path, start=1.0, replies=(), **kw):
    return Env(tmp_path, replies=replies, intervals={"fixed.com": (1.0, 0.0)},
               adaptive={"img.com": AdaptivePace(start, 0.2, 4.0)}, **kw)


def test_adaptive_speeds_up_after_clean_streak(tmp_path):
    e = _adaptive_env(tmp_path)
    for _ in range(19):
        e.session.get("https://img.com/x")
    assert e.session.interval("img.com") == 1.0
    e.session.get("https://img.com/x")
    assert e.session.interval("img.com") == pytest.approx(0.9)
    for _ in range(400):
        e.session.get("https://img.com/x")
    assert e.session.interval("img.com") == pytest.approx(0.2)


def test_adaptive_slows_on_429_and_5xx(tmp_path):
    e = _adaptive_env(tmp_path, start=0.5, replies=[resp(503)])
    assert e.session.get("https://img.com/x").status_code == 503
    assert e.session.interval("img.com") == 1.0
    for _ in range(5):
        e.replies.append(resp(500))
        e.session.get("https://img.com/x")
    assert e.session.interval("img.com") == 4.0
    e2 = _adaptive_env(tmp_path / "b", start=0.5, replies=[resp(429, headers={"Retry-After": "1"})])
    e2.session.get("https://img.com/x")
    assert e2.session.interval("img.com") == 1.0


def test_adaptive_slows_on_block(tmp_path):
    e = _adaptive_env(tmp_path, start=0.5, replies=[resp(302, headers={"Location": SORRY})])
    with pytest.raises(Blocked):
        e.session.get("https://img.com/x")
    assert e.session.interval("img.com") == 1.0


def test_non_adaptive_hosts_unchanged(tmp_path):
    e = _adaptive_env(tmp_path, replies=[resp(503), resp(500)] + [resp()] * 60)
    for _ in range(62):
        e.session.get("https://fixed.com/x")
    assert e.session.interval("fixed.com") == 1.0


def test_derive_keeps_adaptive_config_with_fresh_state(tmp_path):
    e = _adaptive_env(tmp_path, start=0.5, replies=[resp(503)])
    e.session.get("https://img.com/x")
    assert e.session.derive().interval("img.com") == 0.5
