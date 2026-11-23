"""Thread-owned Playwright browser fetch, adapted to requests responses.

Pipeline workers share this transport; all Playwright objects stay on one owner
thread. Fresh browser contexts are retained per proxy and closed with the
transport. User browser profiles and cookies are never imported.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from urllib.parse import urlparse, urlencode

import requests
from playwright.sync_api import Error, TimeoutError, sync_playwright


class PlaywrightTransport:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright-http")
        self._lock = Lock()
        self._closed = False
        self._playwright = None
        self._contexts = {}
        self._browser = None
        self._pages = {}

    def request(self, method, url, **kwargs):
        with self._lock:
            if self._closed:
                raise requests.ConnectionError("Playwright transport is closed")
            future = self._executor.submit(self._request, method, url, kwargs)
        return future.result()

    def _request(self, method, url, kwargs):
        try:
            if self._playwright is None:
                self._playwright = sync_playwright().start()
            if self._browser is None:
                self._browser = self._playwright.chromium.launch(channel="chrome", headless=True)
            proxies = kwargs.get("proxies", {})
            proxy = proxies.get(urlparse(url).scheme)
            if proxy not in self._contexts:
                options = {"proxy": {"server": proxy}} if proxy else {}
                self._contexts[proxy] = self._browser.new_context(**options)
            origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            key = (proxy, origin)
            timeout = kwargs.get("timeout", 30) * 1000
            if key not in self._pages:
                page = self._contexts[proxy].new_page()
                self._pages[key] = page
                response = page.goto(origin, wait_until="domcontentloaded", timeout=timeout)
                if response.status != 200 or "/sorry/" in page.url:
                    result = requests.Response()
                    result.status_code = response.status
                    result.url = page.url
                    result.headers.update(response.headers)
                    result._content = response.body()
                    del self._pages[key]
                    page.close()
                    return result
            page = self._pages[key]
            if kwargs.get("params"):
                url += ("&" if "?" in url else "?") + urlencode(kwargs["params"])
            headers = dict(kwargs.get("headers") or {})
            data = kwargs.get("data")
            if isinstance(data, dict):
                data = urlencode(data)
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            # Chrome supplies origin, referrer and fetch metadata itself.
            headers = {k: v for k, v in headers.items()
                       if k.lower() not in {"origin", "referer"} and not k.lower().startswith("sec-")}
            with page.expect_response(lambda r: r.url == url, timeout=timeout) as received:
                payload = page.evaluate("""async ({url, method, headers, data, timeout}) => {
                    const response = await fetch(url, {method, headers, body: data,
                        credentials: 'omit', redirect: 'manual',
                        signal: AbortSignal.timeout(timeout)});
                    return await response.text();
                }""", {"url": url, "method": method, "headers": headers, "data": data, "timeout": timeout})
            response = received.value
            result = requests.Response()
            result.status_code = response.status
            result.url = response.url
            result.headers.update(response.all_headers())
            result._content = payload.encode("utf-8")
            result.encoding = "utf-8"
            return result
        except TimeoutError as exc:
            raise requests.Timeout(str(exc)) from exc
        except Error as exc:
            # AbortSignal.timeout rejects inside evaluate as a JS DOMException,
            # which Playwright wraps in Error rather than its TimeoutError class.
            if "TimeoutError: signal timed out" in str(exc):
                raise requests.Timeout(str(exc)) from exc
            raise requests.ConnectionError(str(exc)) from exc

    def _dispose(self):
        try:
            try:
                for context in self._contexts.values():
                    context.close()
            finally:
                self._contexts.clear()
                self._pages.clear()
                if self._browser is not None:
                    self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = self._executor.submit(self._dispose)
        try:
            future.result()
        finally:
            self._executor.shutdown(wait=True)
