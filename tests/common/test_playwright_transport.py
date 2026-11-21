import pytest
import requests

from adrenalin.common.http import Cooldown, PacedSession, Blocked


def test_playwright_routing_derive_and_errors(tmp_path, monkeypatch):
    session = PacedSession(Cooldown(tmp_path / 'cooldown.db'), {}, playwright_hosts=('google.test',))
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
            assert derived._playwright_hosts == session._playwright_hosts
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

    transport = PlaywrightTransport()
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

    transport = PlaywrightTransport()
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
