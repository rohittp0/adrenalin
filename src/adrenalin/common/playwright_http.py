"""Thread-owned Playwright browser fetch, adapted to requests responses.

Pipeline workers share this transport; all Playwright objects stay on one owner
thread. Fresh browser contexts are retained per proxy and closed with the
transport. User browser profiles and cookies are never imported.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from threading import Lock, Event
from time import monotonic, time
from pathlib import Path
import hashlib
import json
import os
import tempfile
from urllib.parse import urlparse, urlencode

import requests
from playwright.sync_api import Error, TimeoutError, sync_playwright


class PlaywrightTransport:
    def __init__(self, *, solve_captcha=False, log=None, cookie_dir=None):
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
        self._cookie_dir = Path(cookie_dir) if cookie_dir is not None else None

    def _cookie_path(self, proxy):
        if self._cookie_dir is None:
            return None
        key = hashlib.sha256((proxy or "direct").encode()).hexdigest()
        return self._cookie_dir / f"{key}.json"

    @staticmethod
    def _exemptions(state):
        return [c for c in state.get("cookies", [])
                if c.get("name") == "GOOGLE_ABUSE_EXEMPTION"
                and c.get("domain", "").lstrip(".") in {"google.com", "adstransparency.google.com"}
                and isinstance(c.get("expires"), (int, float)) and c["expires"] > time()]

    def _load_exemption(self, proxy):
        path = self._cookie_path(proxy)
        if path is None:
            return None
        try:
            cookies = self._exemptions(json.loads(path.read_text()))
            return {"cookies": cookies, "origins": []} if cookies else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save_exemption(self, proxy, state):
        path = self._cookie_path(proxy)
        if path is None:
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".exemption-")
            with os.fdopen(fd, "w") as output:
                json.dump({"cookies": self._exemptions(state), "origins": []}, output)
            os.replace(temporary, path)  # mkstemp creates an owner-only file
        except OSError:
            self._log("warning: could not save CAPTCHA exemption for reuse")
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

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
            if proxy not in self._storage:
                saved = self._load_exemption(proxy)
                if saved:
                    self._storage[proxy] = saved
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
        for proxy, state in states.items():
            self._save_exemption(proxy, state)
        self._storage.update(states)
        self._headed = headed

    def _request(self, method, url, kwargs):
        proxy = kwargs.get("proxies", {}).get(urlparse(url).scheme)
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        key = (proxy, origin)
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
            if "TypeError: Failed to fetch" in str(exc):
                # Reload the service origin on the next paced RPC attempt. A stale
                # page or hidden block must not poison every subsequent Creative.
                page = self._pages.pop(key, None)
                if page is not None:
                    with suppress(Error):
                        page.close()
            raise requests.ConnectionError(str(exc)) from exc

    def _dispose(self):
        try:
            try:
                for proxy, context in self._contexts.items():
                    try:
                        if self._cookie_dir is not None:
                            self._save_exemption(proxy, context.storage_state())
                    except Error:
                        self._log("warning: browser closed before CAPTCHA exemption could be saved")
                    finally:
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
