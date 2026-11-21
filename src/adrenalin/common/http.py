"""Block-aware paced HTTP session with a persisted per-host cooldown."""

from __future__ import annotations

import contextlib
import itertools
import random
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse

import requests
from adrenalin.common.playwright_http import PlaywrightTransport
from urllib3.exceptions import NameResolutionError

from adrenalin.common.cache import ExtractionCache

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class Blocked(Exception):
    def __init__(self, host: str, until: float, *, saved_cooldown: bool = False):
        detail = " (request skipped during saved cooldown)" if saved_cooldown else ""
        super().__init__(f"{host} blocked until {_clock_time(until)}{detail}")
        self.host = host
        self.until = until


def _clock_time(ts: float) -> str:
    try:
        return f"{datetime.fromtimestamp(ts):%H:%M:%S}"
    except (OverflowError, OSError, ValueError):
        return str(ts)


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


def is_block_response(resp: requests.Response, read_body: bool = True) -> bool:
    if "/sorry/" in (resp.url or "") or "/sorry/" in resp.headers.get("Location", ""):
        return True
    if read_body and "text/html" in resp.headers.get("Content-Type", "").lower():
        text = resp.content[:4000].decode("utf-8", errors="ignore").lower()
        return "recaptcha" in text
    return False


def describe_response(resp: requests.Response, read_body: bool = True) -> str:
    """One line on what a response looked like, for logging why it was taken as a block."""
    parts = [f"HTTP {resp.status_code}", resp.url or ""]
    if loc := resp.headers.get("Location"):
        parts.append(f"Location={loc}")
    parts.append(resp.headers.get("Content-Type", "no content type"))
    if read_body:
        parts.append(f"body={resp.content[:200]!r}")
    return " ".join(parts)


def _retry_after_seconds(value: str, now: float) -> float:
    value = value.strip()
    if value.isascii() and value.isdecimal():
        seconds = float(value)
        return seconds if isfinite(seconds) else 0.0
    if value:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, retry_at.timestamp() - now)
        except (ValueError, OverflowError, OSError):
            pass
    return 0.0


@dataclass(frozen=True)
class AdaptivePace:
    """A host's pace that adapts: `speedup` x after `clean_streak` clean responses in a row (down to
    `floor`), `slowdown` x on a 429, 5xx, block, timeout or connection error (up to `ceiling`). Intervals are in
    seconds."""
    start: float
    floor: float
    ceiling: float
    speedup: float = 0.9
    slowdown: float = 2.0
    clean_streak: int = 20


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
        log: Callable[[str], None] | None = None,
        adaptive: Mapping[str, AdaptivePace] = {},
        playwright_hosts: Sequence[str] = (),
    ):
        self._playwright_hosts = frozenset(playwright_hosts)
        self._playwright_session = PlaywrightTransport() if playwright_hosts else None
        self._adaptive = dict(adaptive)
        self._adaptive_state: dict[str, list[float]] = {h: [a.start, 0] for h, a in self._adaptive.items()}
        self._cooldown = cooldown
        self._log = log
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
        self._transport = transport or self._request_transport
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last: dict[str, float] = {}

    def _request_transport(self, method: str, url: str, **kwargs):
        if urlparse(url).hostname not in self._playwright_hosts:
            return self._session.request(method, url, **kwargs)
        return self._playwright_session.request(method, url, **kwargs)

    def _host_lock(self, host: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def interval(self, host: str) -> float:
        """The current base interval of a host."""
        with self._host_lock(host):
            if host in self._adaptive_state:
                return self._adaptive_state[host][0]
        return self._intervals.get(host, self._default_interval)[0]

    def _record(self, host: str, clean: bool) -> None:
        """Feed an adaptive host's pace one response: clean, or a 429, 5xx, block or network error."""
        cfg = self._adaptive.get(host)
        if cfg is None:
            return
        with self._host_lock(host):
            state = self._adaptive_state[host]
            if not clean:
                state[0], state[1] = min(cfg.ceiling, state[0] * cfg.slowdown), 0
                return
            state[1] += 1
            if state[1] >= cfg.clean_streak:
                state[0], state[1] = max(cfg.floor, state[0] * cfg.speedup), 0

    def _pace(self, host: str) -> None:
        base, jitter = self._intervals.get(host, self._default_interval)
        with self._host_lock(host):
            if host in self._adaptive_state:
                base = self._adaptive_state[host][0]
                jitter = 0.3 * base
            last = self._last.get(host)
            if last is not None:
                delay = base - jitter + 2 * jitter * self._rng()
                wait = last + delay - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last[host] = self._clock()

    def _wait_or_raise(self, host: str) -> None:
        while True:
            until = self._cooldown.until(host)
            remaining = until - self._clock()
            if remaining <= 0:
                return
            if not self._wait_on_block:
                raise Blocked(host, until, saved_cooldown=True)
            if self._log is not None:
                self._log(f"{host}: waiting for cooldown until {_clock_time(until)}")
            self._sleep(remaining)

    def _block(self, host: str, retry_after: float) -> float:
        return self._cooldown.block(host, max(self._block_seconds, retry_after), now=self._clock())

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
            try:
                resp = self._transport(method, url, **send)
            except requests.RequestException:
                self._record(host, clean=False)  # a timeout or dropped connection is a sign to slow down too
                raise

            # stream=True callers (the host check) never download the body, so only the URL tells a block
            read_body = not kwargs.get("stream")
            if is_block_response(resp, read_body=read_body):
                self._record(host, clean=False)
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After", ""), self._clock())
                until = self._block(host, retry_after)
                self._warn(f"{host} sent a block page ({describe_response(resp, read_body)})")
                if self._wait_on_block and block_waits < self._max_retries:
                    block_waits += 1
                    continue
                raise Blocked(host, until)

            self._record(host, clean=resp.status_code != 429 and resp.status_code < 500)
            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After", ""), self._clock())
                if attempt >= self._max_retries:
                    self._warn(f"{host} still rate-limits after {attempt} backoffs ({describe_response(resp)})")
                    raise Blocked(host, self._block(host, retry_after))
                backoff = max(min(60 * 2**attempt, 900), retry_after)
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
            sleep=self._sleep, clock=self._clock, rng=self._rng, log=self._log,
            adaptive=self._adaptive, playwright_hosts=self._playwright_hosts,
        )

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log(f"warning: {message}")

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._session.close()
        if self._playwright_session is not None:
            self._playwright_session.close()


def _dns_failure(e: BaseException) -> bool:
    """Whether a requests error was caused by a host name that does not resolve."""
    stack, seen = [e], set()
    while stack:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, (socket.gaierror, NameResolutionError)):
            return True
        stack += [x.__cause__, x.__context__, getattr(x, "reason", None)]
        stack += [a for a in getattr(x, "args", ()) if isinstance(a, BaseException)]
    return False


class HostChecker:
    """Whether a host name exists, from `GET https://<host>/` (body never read). Only a DNS resolution
    failure means missing: any HTTP response, a block page, and TLS, refused or timeout errors all count as
    existing, so a slow or misconfigured site is never called a misread. Before saying missing, the other
    `www.` form is tried. Use a fail-fast session (`derive(wait_on_block=False, max_retries=0)`).
    Each name is requested once per checker; names that answered are also remembered in `cache` across runs.
    Misses are not, since a resolver outage would otherwise mark real hosts as missing for good."""

    def __init__(self, session: PacedSession, cache: ExtractionCache | None = None, timeout: float = 5.0,
                 log: Callable[[str], None] | None = None):
        self._s = session
        self._cache = cache
        self._timeout = timeout
        self._log = log
        self._known: dict[str, bool] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._reported: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, host: str) -> bool:
        host = host.strip().strip(".").lower()
        if self._exists(host):
            return True
        other = host[4:] if host.startswith("www.") else f"www.{host}"
        if "." in other.removeprefix("www.") and self._exists(other):
            return True
        with self._lock:
            first = host.removeprefix("www.") not in self._reported
            self._reported.add(host.removeprefix("www."))
        if first and self._log is not None:
            self._log(f"warning: {host} does not resolve (DNS); treated as misread by OCR")
        return False

    def _exists(self, host: str) -> bool:
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
        exists, answered = self._request(host)
        if answered and self._cache is not None:
            self._cache.put(key, "domain", "", {"exists": True})
        return exists

    def _request(self, host: str) -> tuple[bool, bool]:
        """(exists, the host answered)."""
        try:
            resp = self._s.get(f"https://{host}/", timeout=self._timeout, allow_redirects=True, stream=True)
        except Blocked:
            return True, False
        except requests.RequestException as e:
            return not _dns_failure(e), False
        except ValueError:  # not a valid host name
            return False, False
        with contextlib.suppress(Exception):
            resp.close()  # the body is never read
        return True, True
