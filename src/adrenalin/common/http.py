"""Block-aware paced HTTP session with a persisted per-host cooldown."""

from __future__ import annotations

import contextlib
import itertools
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

import requests

from adrenalin.common.cache import ExtractionCache

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Blocked(Exception):
    def __init__(self, host: str, until: float):
        super().__init__(f"{host} blocked until {until}")
        self.host = host
        self.until = until


class Cooldown:
    """Per-host block deadlines persisted in SQLite. Thread-safe."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = self._connect()
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS cooldown (host TEXT PRIMARY KEY, until REAL)"
                )
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30)

    def until(self, host: str) -> float:
        with self._lock:
            db = self._connect()
            try:
                row = db.execute("SELECT until FROM cooldown WHERE host = ?", (host,)).fetchone()
            finally:
                db.close()
        return float(row[0]) if row else 0.0

    def remaining(self, host: str, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return max(0.0, self.until(host) - now)

    def block(self, host: str, seconds: float, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        until = now + seconds
        with self._lock:
            db = self._connect()
            try:
                with db:
                    db.execute(
                        "INSERT INTO cooldown(host, until) VALUES (?, ?) "
                        "ON CONFLICT(host) DO UPDATE SET until = MAX(until, excluded.until)",
                        (host, until),
                    )
            finally:
                db.close()
        return max(until, self.until(host))


def is_block_response(resp: requests.Response) -> bool:
    if "/sorry/" in (resp.url or "") or "/sorry/" in resp.headers.get("Location", ""):
        return True
    if "text/html" in resp.headers.get("Content-Type", "").lower():
        text = resp.content[:4000].decode("utf-8", errors="ignore").lower()
        return "recaptcha" in text
    return False


class PacedSession:
    def __init__(
        self,
        cooldown: Cooldown,
        intervals: Mapping[str, tuple[float, float]],
        *,
        default_interval: tuple[float, float] = (0.5, 0.0),
        proxies: Sequence[str] = (),
        wait_on_block: bool = False,
        max_retries: int = 3,
        block_seconds: float = 900.0,
        transport: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ):
        self._cooldown = cooldown
        self._custom_transport = transport
        self._proxy_list = tuple(proxies)
        self._intervals = dict(intervals)
        self._default_interval = default_interval
        self._proxies = itertools.cycle(proxies) if proxies else None
        self._proxy_lock = threading.Lock()
        self._wait_on_block = wait_on_block
        self._max_retries = max_retries
        self._block_seconds = block_seconds
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"}
        )
        self._transport = transport or self._session.request
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last: dict[str, float] = {}

    def _host_lock(self, host: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def _pace(self, host: str) -> None:
        base, jitter = self._intervals.get(host, self._default_interval)
        with self._host_lock(host):
            last = self._last.get(host)
            if last is not None:
                delay = base - jitter + 2 * jitter * self._rng()
                wait = last + delay - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last[host] = self._clock()

    def _wait_or_raise(self, host: str) -> None:
        while True:
            remaining = self._cooldown.remaining(host, now=self._clock())
            if remaining <= 0:
                return
            if not self._wait_on_block:
                raise Blocked(host, self._cooldown.until(host))
            self._sleep(remaining)

    def _block(self, host: str) -> float:
        return self._cooldown.block(host, self._block_seconds, now=self._clock())

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        host = urlparse(url).hostname or ""
        kwargs.setdefault("allow_redirects", False)
        kwargs.setdefault("timeout", 30)
        attempt = 0
        block_waits = 0
        while True:
            self._wait_or_raise(host)
            self._pace(host)
            send = dict(kwargs)
            if self._proxies is not None:
                with self._proxy_lock:
                    p = next(self._proxies)
                send["proxies"] = {"http": p, "https": p}
            resp = self._transport(method, url, **send)

            if is_block_response(resp):
                until = self._block(host)
                if self._wait_on_block and block_waits < self._max_retries:
                    block_waits += 1
                    continue
                raise Blocked(host, until)

            if resp.status_code == 429:
                if attempt >= self._max_retries:
                    raise Blocked(host, self._block(host))
                backoff = min(60 * 2**attempt, 900)
                try:
                    backoff = max(backoff, float(resp.headers.get("Retry-After", 0)))
                except ValueError:
                    pass
                self._sleep(backoff)
                attempt += 1
                continue
            return resp

    def derive(self, *, wait_on_block: bool | None = None, max_retries: int | None = None) -> PacedSession:
        """A new session sharing this one's cooldown, intervals, proxies, transport, sleep and clock,
        with its own block/retry policy and its own pacing state. Close it separately."""
        return PacedSession(
            self._cooldown, self._intervals,
            default_interval=self._default_interval, proxies=self._proxy_list,
            wait_on_block=self._wait_on_block if wait_on_block is None else wait_on_block,
            max_retries=self._max_retries if max_retries is None else max_retries,
            block_seconds=self._block_seconds, transport=self._custom_transport,
            sleep=self._sleep, clock=self._clock, rng=self._rng,
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._session.close()


class HostChecker:
    """Whether a host name exists, from one `GET https://<host>/`: any HTTP response (any status, or a
    block page) means yes; a DNS, connection, TLS or timeout error means no. Use a fail-fast session
    (`derive(wait_on_block=False, max_retries=0)`). Each host is requested once per checker; hosts that
    exist are also remembered in `cache` across runs. Hosts that don't are not, since a network outage
    would otherwise mark real hosts as missing for good."""

    def __init__(self, session: PacedSession, cache: ExtractionCache | None = None, timeout: float = 5.0):
        self._s = session
        self._cache = cache
        self._timeout = timeout
        self._known: dict[str, bool] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def __call__(self, host: str) -> bool:
        host = host.strip().strip(".").lower()
        with self._lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:  # concurrent extract workers asking for one host make one request
            if host not in self._known:
                self._known[host] = self._check(host)
            return self._known[host]

    def _check(self, host: str) -> bool:
        key = f"domain:{host}"
        if self._cache is not None and self._cache.get(key, "domain") is not None:
            return True
        exists = self._request(host)
        if exists and self._cache is not None:
            self._cache.put(key, "domain", "", {"exists": True})
        return exists

    def _request(self, host: str) -> bool:
        try:
            resp = self._s.get(f"https://{host}/", timeout=self._timeout, allow_redirects=True, stream=True)
        except Blocked:
            return True
        except (requests.RequestException, ValueError):
            return False
        with contextlib.suppress(Exception):
            resp.close()  # the body is never read
        return True
