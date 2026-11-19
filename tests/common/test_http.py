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
