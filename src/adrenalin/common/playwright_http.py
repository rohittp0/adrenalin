"""Thread-owned Playwright browser fetch, adapted to requests responses.

Pipeline workers share this transport; all Playwright objects stay on one owner
thread. Fresh browser contexts are retained per proxy and closed with the
transport. User browser profiles and cookies are never imported.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Event
from time import monotonic
from urllib.parse import urlparse, urlencode

import requests
from playwright.sync_api import Error, TimeoutError, sync_playwright


class PlaywrightTransport:
    def __init__(self, *, solve_captcha=False, log=None):
        self._solve_captcha = solve_captcha
        self._log = log or (lambda message: None)
        self._stop = Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright-http")
        self._lock = Lock()
        self._closed = False
        self._playwright = None
        self._contexts = {}
        self._browser = None
        self._pages = {}
        self._headed = False
        self._storage = {}

    def request(self, method, url, **kwargs):
        with self._lock:
            if self._closed:
                raise requests.ConnectionError("Playwright transport is closed")
            future = self._executor.submit(self._request_with_handoff, method, url, kwargs)
        return future.result()

    def _request_with_handoff(self, method, url, kwargs):
        try:
            return self._handoff_request(method, url, kwargs)
        finally:
            # Also dismiss the visible browser if navigation or the handoff fails.
            if self._headed:
                self._switch_browser(headed=False)

    def _handoff_request(self, method, url, kwargs):
        response = self._request(method, url, kwargs)
        if not self._solve_captcha:
            return response
        blocked = "/sorry/" in response.url or "/sorry/" in response.headers.get("Location", "")
        if self._solve_captcha and blocked:
            origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            proxy = kwargs.get("proxies", {}).get(urlparse(url).scheme)
            key = (proxy, origin)
            self._switch_browser(headed=True)
            self._ensure_context(proxy)
            page = self._pages.get(key)
            if page is None:
                page = self._contexts[proxy].new_page()
                self._pages[key] = page
            # Navigate to the service root, not a POST RPC redirect URL. Google
            # supplies the challenge and returns here when the user completes it.
            page.goto(origin, wait_until="domcontentloaded", timeout=30000)
            page.bring_to_front()
            self._log("CAPTCHA: complete the challenge in the scraper's Chrome window. "
                      "Waiting up to 10 minutes; the request resumes automatically.")
            deadline = monotonic() + 600
            while not self._stop.is_set() and monotonic() < deadline:
                if page.is_closed():
                    break
                current = urlparse(page.url)
                if f"{current.scheme}://{current.netloc}" == origin and "/sorry/" not in current.path:
                    page.wait_for_load_state("domcontentloaded")
                    self._switch_browser(headed=False)
                    self._log("CAPTCHA completed; Chrome window closed. Resuming in the background.")
                    return self._request(method, url, kwargs)
                page.wait_for_timeout(500)
            self._pages.pop(key, None)
            if not page.is_closed():
                page.close()
            self._switch_browser(headed=False)
            self._log("CAPTCHA handoff ended without completion; preserving the blocked request.")
        return response

    def _ensure_context(self, proxy):
        if self._browser is None:
            self._browser = self._playwright.chromium.launch(channel="chrome", headless=not self._headed)
        if proxy not in self._contexts:
            options = {"proxy": {"server": proxy}} if proxy else {}
            if proxy in self._storage:
                options["storage_state"] = self._storage[proxy]
            self._contexts[proxy] = self._browser.new_context(**options)

    def _switch_browser(self, *, headed):
        if self._headed == headed:
            return
        # Preserve cookies/local storage in memory, independently for each proxy.
        # A headless process cannot acquire a visible window: restart Chrome.
        states = {proxy: context.storage_state() for proxy, context in self._contexts.items()}
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            self._contexts.clear()
            self._pages.clear()
        self._storage.update(states)
        self._headed = headed

    def _request(self, method, url, kwargs):
        try:
            if self._playwright is None:
                self._playwright = sync_playwright().start()
            proxies = kwargs.get("proxies", {})
            proxy = proxies.get(urlparse(url).scheme)
            self._ensure_context(proxy)
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
                        credentials: 'same-origin', redirect: 'manual',
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
            self._storage.clear()
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def close(self):
        self._stop.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = self._executor.submit(self._dispose)
        try:
            future.result()
        finally:
            self._executor.shutdown(wait=True)
