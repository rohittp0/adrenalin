import pytest
import requests

from adrenalin.common.http import Blocked, Challenge, Cooldown, HostPolicy, PacedSession
from adrenalin.tools.ads_transparency.hosts import GOOGLE_CHALLENGE, HOSTS
from adrenalin.tools.ads_transparency.rpc import DETAIL_URL, HOST

GOOGLE_POLICY = HostPolicy(browser=True, challenge=GOOGLE_CHALLENGE)


def google_policies(host):
    return GOOGLE_POLICY


def sorry(resp, read_body):
    return '/sorry/ redirect' if '/sorry/' in resp.headers.get('Location', '') else None


def test_playwright_routing_derive_and_errors(tmp_path, monkeypatch):
    session = PacedSession(Cooldown(tmp_path / 'cooldown.db'), {'google.test': HostPolicy(browser=True, blocked=sorry)})
    response = requests.Response()
    response.status_code = 200
    response.url = 'https://google.test/'
    response._content = b'{}'
    calls = []
    def send(method, url, **kwargs):
        calls.append(kwargs)
        return response
    monkeypatch.setattr(session._playwright_session, 'request', send)
    monkeypatch.setattr(session._session, 'request', lambda *a, **kw: response)
    try:
        assert session.get('https://google.test/') is response
        assert calls[0]['allow_redirects'] is False
        assert session.get('https://other.test/') is response
        assert len(calls) == 1
        derived = session.derive()
        try:
            assert derived._hosts == session._hosts
            assert derived._playwright_session is not session._playwright_session
        finally:
            derived.close()
        def timeout(*a, **kw):
            raise requests.Timeout('timeout')
        monkeypatch.setattr(session._playwright_session, 'request', timeout)
        with pytest.raises(requests.Timeout):
            session.get('https://google.test/')
        monkeypatch.setattr(session._playwright_session, 'request', send)
        response.headers['Location'] = 'https://google.test/sorry/'
        with pytest.raises(Blocked):
            session.get('https://google.test/')
        with pytest.raises(Blocked):
            session.get('https://google.test/')
        assert len(calls) == 2
    finally:
        session.close()


def test_transport_owns_one_thread_and_closes_after_error(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import get_ident
    from adrenalin.common.playwright_http import PlaywrightTransport

    transport = PlaywrightTransport(policies=google_policies)
    owners = []
    def request(method, url, kwargs):
        owners.append(get_ident())
        if url == 'fail':
            raise requests.Timeout('test')
        return url
    def dispose():
        owners.append(get_ident())
    monkeypatch.setattr(transport, '_request', request)
    monkeypatch.setattr(transport, '_dispose', dispose)
    try:
        with ThreadPoolExecutor(max_workers=3) as callers:
            assert list(callers.map(lambda x: transport.request('GET', str(x)), range(6))) == list(map(str, range(6)))
        with pytest.raises(requests.Timeout):
            transport.request('GET', 'fail')
    finally:
        transport.close()
    transport.close()
    assert len(set(owners)) == 1
    assert owners[0] != get_ident()
    with pytest.raises(requests.ConnectionError, match='closed'):
        transport.request('GET', 'closed')


def test_cleanup_stops_browser_even_when_context_close_fails():
    from types import SimpleNamespace
    from adrenalin.common.playwright_http import PlaywrightTransport

    transport = PlaywrightTransport(policies=google_policies)
    closed = []
    def fail():
        raise RuntimeError('context already gone')
    transport._contexts['test'] = SimpleNamespace(close=fail)
    transport._browser = SimpleNamespace(close=lambda: closed.append('browser'))
    transport._playwright = SimpleNamespace(stop=lambda: closed.append('playwright'))
    with pytest.raises(RuntimeError, match='context already gone'):
        transport.close()
    assert closed == ['browser', 'playwright']
    assert transport._executor._shutdown


@pytest.mark.parametrize('message, expected', [
    ('Page.evaluate: TimeoutError: signal timed out', requests.Timeout),
    ('Page.evaluate: TypeError: Failed to fetch', requests.ConnectionError),
])
def test_javascript_fetch_error_classification(message, expected):
    from types import SimpleNamespace
    from contextlib import nullcontext
    from playwright.sync_api import Error
    from adrenalin.common.playwright_http import PlaywrightTransport

    def evaluate(*args):
        raise Error(message)
    transport = PlaywrightTransport(policies=google_policies)
    transport._playwright = SimpleNamespace(stop=lambda: None)
    transport._browser = SimpleNamespace(close=lambda: None)
    transport._contexts[None] = SimpleNamespace(close=lambda: None)
    transport._pages[(None, 'https://example.test')] = SimpleNamespace(
        expect_response=lambda *args, **kwargs: nullcontext(), evaluate=evaluate, close=lambda: None)
    try:
        with pytest.raises(expected):
            transport.request('POST', 'https://example.test/rpc', data={'f.req': '{}'})
        if expected is requests.ConnectionError:
            assert (None, 'https://example.test') not in transport._pages
    finally:
        transport.close()


@pytest.mark.parametrize('solved', [True, False])
def test_captcha_handoff_replays_same_request_only_after_return(monkeypatch, solved):
    from types import SimpleNamespace
    from adrenalin.common.playwright_http import PlaywrightTransport
    transport = PlaywrightTransport(policies=google_policies, solve_captcha=True)
    blocked = requests.Response()
    blocked.status_code = 429
    blocked.url = 'https://www.google.com/sorry/index'
    blocked._content = b'challenge'
    success = requests.Response()
    success.status_code = 200
    success.url = 'https://example.test/rpc'
    success._content = b'{}'
    calls = []
    def request(method, url, kwargs):
        calls.append((method, url, kwargs))
        return blocked if len(calls) == 1 else success
    page = SimpleNamespace(url=blocked.url, goto=lambda *a, **k: None,
                           bring_to_front=lambda: None, is_closed=lambda: not solved)
    def tick(ms):
        page.url = 'https://example.test/?google_abuse=temporary'
    page.wait_for_timeout = tick
    page.wait_for_load_state = lambda *a: None
    switches = []
    monkeypatch.setattr(transport, '_switch_browser', lambda **kw: switches.append(kw['headed']))
    monkeypatch.setattr(transport, '_ensure_context', lambda proxy: None)
    transport._pages[(None, 'https://example.test')] = page
    monkeypatch.setattr(transport, '_request', request)
    try:
        result = transport.request('POST', 'https://example.test/rpc', data={'f.req': 'cursor'})
        assert result is (success if solved else blocked)
        assert len(calls) == (2 if solved else 1)
        assert switches == [True, False]
        if solved:
            assert calls[0] == calls[1]
    finally:
        transport.close()


@pytest.mark.parametrize('outcome', ['solved', 'unsolved', 'rate_limited_after_solve'])
def test_captcha_handoff_preserves_adaptive_pace_only_when_successful(tmp_path, monkeypatch, outcome):
    from types import SimpleNamespace

    now = 0.0
    def sleep(seconds):
        nonlocal now
        now += seconds

    session = PacedSession(Cooldown(tmp_path / 'cooldown.db'), HOSTS, solve_captcha=True,
                           clock=lambda: now, sleep=sleep, rng=lambda: 0.5, max_retries=0)
    transport = session._playwright_session
    success = requests.Response()
    success.status_code = 200
    success.url = DETAIL_URL
    success._content = b'{}'
    blocked = requests.Response()
    blocked.status_code = 429
    blocked.url = 'https://www.google.com/sorry/index'
    limited = requests.Response()
    limited.status_code = 429
    limited.url = DETAIL_URL
    blocked._content = b'challenge'
    limited._content = b'rate limited'
    origin = f'https://{HOST}'
    page = SimpleNamespace(url=blocked.url, goto=lambda *a, **k: None,
                           bring_to_front=lambda: None, is_closed=lambda: outcome == 'unsolved',
                           wait_for_load_state=lambda *a: None)
    page.wait_for_timeout = lambda ms: setattr(page, 'url', origin)
    monkeypatch.setattr(transport, '_switch_browser', lambda **kw: None)
    monkeypatch.setattr(transport, '_ensure_context', lambda proxy: None)
    monkeypatch.setattr(transport, '_request', lambda *a: success)
    try:
        for _ in range(20):
            session.post(DETAIL_URL)
        before = session.interval(HOST)
        assert before == pytest.approx(2.7)
        transport._pages[(None, origin)] = page
        replies = iter([blocked, limited if outcome == 'rate_limited_after_solve' else success])
        monkeypatch.setattr(transport, '_request', lambda *a: next(replies))
        if outcome == 'solved':
            assert session.post(DETAIL_URL) is success
            assert session.interval(HOST) == before
            assert session._cooldown.remaining(HOST, now=now) == 0
        else:
            with pytest.raises(Blocked):
                session.post(DETAIL_URL)
            assert session.interval(HOST) == before * 2
    finally:
        session.close()


def test_browser_switch_preserves_proxy_sessions_in_memory():
    from types import SimpleNamespace
    from adrenalin.common.playwright_http import PlaywrightTransport
    transport = PlaywrightTransport(policies=google_policies, solve_captcha=True)
    launches, restored, closed = [], [], []
    state = {'cookies': [{'name': 'exemption', 'value': 'test'}], 'origins': []}
    def context(**options):
        restored.append(options)
        return SimpleNamespace(storage_state=lambda: state, close=lambda: None)
    def launch(**options):
        launches.append(options)
        return SimpleNamespace(new_context=context, close=lambda: closed.append(True))
    transport._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=lambda: None)
    try:
        transport._ensure_context('http://proxy.test:8080')
        transport._switch_browser(headed=True)
        transport._ensure_context('http://proxy.test:8080')
        transport._switch_browser(headed=False)
        transport._ensure_context('http://proxy.test:8080')
        assert [x['headless'] for x in launches] == [True, False, True]
        assert restored[1]['storage_state'] == restored[2]['storage_state'] == state
        assert restored[2]['proxy'] == {'server': 'http://proxy.test:8080'}
        assert len(closed) == 2
    finally:
        transport.close()
    assert len(closed) == 3
    assert not transport._storage


def test_exemption_reused_after_restart_with_expiry_and_proxy_isolation(tmp_path):
    from time import time
    from adrenalin.common.playwright_http import PlaywrightTransport
    cookie = dict(name='GOOGLE_ABUSE_EXEMPTION', value='test-exemption', domain='.google.com',
                  path='/', expires=time() + 600, httpOnly=True, secure=True, sameSite='None')
    first = PlaywrightTransport(policies=google_policies, cookie_dir=tmp_path)
    try:
        first._challenges[None] = [GOOGLE_CHALLENGE]
        first._save_exemption(None, {'cookies': [cookie, {**cookie, 'name': 'SID'}]})
    finally:
        first.close()
    second = PlaywrightTransport(policies=google_policies, cookie_dir=tmp_path)
    try:
        second._challenges[None] = second._challenges['http://proxy.test'] = [GOOGLE_CHALLENGE]
        assert second._load_exemption(None)['cookies'] == [cookie]
        assert second._load_exemption('http://proxy.test') is None
        assert second._cookie_path(None).stat().st_mode & 0o777 == 0o600
        second._save_exemption(None, {'cookies': [{**cookie, 'expires': time() - 1}]})
        assert second._load_exemption(None) is None
        second._cookie_path(None).write_text('broken')
        assert second._load_exemption(None) is None
    finally:
        second.close()


def test_challenge_rule_comes_from_policy(monkeypatch):
    from types import SimpleNamespace
    from adrenalin.common.playwright_http import PlaywrightTransport
    challenge = Challenge(shown=lambda url: '/blocked/' in url)
    transport = PlaywrightTransport(policies=lambda host: HostPolicy(browser=True, challenge=challenge),
                                    solve_captcha=True)
    blocked = requests.Response()
    blocked.status_code = 200
    blocked.url = 'https://site.test/blocked/now'
    success = requests.Response()
    success.status_code = 200
    success.url = 'https://site.test/rpc'
    calls = []
    def request(method, url, kwargs):
        calls.append(url)
        return blocked if len(calls) == 1 else success
    page = SimpleNamespace(url=blocked.url, goto=lambda *a, **k: None, bring_to_front=lambda: None,
                           is_closed=lambda: False, wait_for_load_state=lambda *a: None)
    def tick(ms):
        page.url = 'https://site.test/'
    page.wait_for_timeout = tick
    monkeypatch.setattr(transport, '_switch_browser', lambda **kw: None)
    monkeypatch.setattr(transport, '_ensure_context', lambda proxy: None)
    transport._pages[(None, 'https://site.test')] = page
    monkeypatch.setattr(transport, '_request', request)
    try:
        assert transport.request('GET', 'https://site.test/rpc') is success
        assert len(calls) == 2
    finally:
        transport.close()


def test_host_without_challenge_never_hands_off(monkeypatch):
    from adrenalin.common.playwright_http import PlaywrightTransport
    transport = PlaywrightTransport(policies=lambda host: HostPolicy(browser=True), solve_captcha=True)
    sorry_page = requests.Response()
    sorry_page.status_code = 429
    sorry_page.url = 'https://site.test/sorry/index'
    switches = []
    monkeypatch.setattr(transport, '_request', lambda method, url, kwargs: sorry_page)
    monkeypatch.setattr(transport, '_switch_browser', lambda **kw: switches.append(kw))
    try:
        assert transport.request('GET', 'https://site.test/rpc') is sorry_page
        assert switches == []
    finally:
        transport.close()


def test_only_policy_cookies_persist(tmp_path):
    from time import time
    from adrenalin.common.playwright_http import PlaywrightTransport
    challenge = Challenge(shown=lambda url: False, keep_cookies=frozenset({'KEEP'}),
                          cookie_domains=frozenset({'site.test'}))
    cookie = dict(name='KEEP', value='v', domain='.site.test', path='/', expires=time() + 600)
    transport = PlaywrightTransport(policies=lambda host: HostPolicy(challenge=challenge), cookie_dir=tmp_path)
    try:
        transport._challenges[None] = [challenge]
        transport._save_exemption(None, {'cookies': [
            cookie, {**cookie, 'name': 'OTHER'}, {**cookie, 'expires': time() - 1},
            {**cookie, 'domain': 'elsewhere.test'}]})
        assert transport._load_exemption(None)['cookies'] == [cookie]
    finally:
        transport.close()
