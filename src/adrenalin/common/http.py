"""Block-aware paced HTTP session with a persisted per-host cooldown."""

from __future__ import annotations

import contextlib
import itertools
import logging
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
from adrenalin.common.output import cache_dir

log = logging.getLogger(__name__)

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
    `floor`). A 429 or block slows immediately; `failure_streak` consecutive network/5xx
    failures slow by `slowdown` (up to `ceiling`). Intervals are in seconds."""
    start: float
    floor: float
    ceiling: float
    speedup: float = 0.9
    slowdown: float = 2.0
    clean_streak: int = 20
    failure_streak: int = 3


BlockCheck = Callable[[requests.Response, bool], "str | None"]


def no_block(resp: requests.Response, read_body: bool) -> str | None:
    """A block check that never sees a block."""
    return None


@dataclass(frozen=True)
class Challenge:
    """A host's human-solvable challenge page. `shown(url)` says whether a URL is one. Cookies are persisted
    only when both name and domain match (domain compared with the leading `.` stripped)."""
    shown: Callable[[str], bool]
    keep_cookies: frozenset[str] = frozenset()
    cookie_domains: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HostPolicy:
    """How one host (or `.suffix` family of hosts) is paced, fetched and checked for blocks.
    `blocked(response, read_body)` returns a reason when the response is a block, else None."""
    pace: AdaptivePace | tuple[float, float] = (0.5, 0.0)
    browser: bool = False
    blocked: BlockCheck = no_block
    challenge: Challenge | None = None


class PacedSession:
    def __init__(
        self,
        cooldown: Cooldown,
        hosts: Mapping[str, HostPolicy],
        *,
        default: HostPolicy = HostPolicy(),
        proxies: Sequence[str] = (),
        wait_on_block: bool = False,
        max_retries: int = 3,
        block_seconds: float = 900.0,
        transport: Callable[..., requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        solve_captcha: bool = False,
        cookie_dir: Path | None = None,
    ):
        self._cookie_dir = cookie_dir
        self._solve_captcha = solve_captcha
        self._hosts = dict(hosts)
        self._default = default
        self._suffixes = sorted((k for k in self._hosts if k.startswith(".")), key=len, reverse=True)
        needs_browser = default.browser or any(p.browser for p in self._hosts.values())
        self._playwright_session = (PlaywrightTransport(
            policies=lambda host: self._policy(self.policy_key(host)),
            solve_captcha=solve_captcha, cookie_dir=cookie_dir) if needs_browser else None)
        self._adaptive_state: dict[str, list[float]] = {}
        self._failure_streaks: dict[str, int] = {}
        self._cooldown = cooldown
        self._custom_transport = transport
        self._proxy_list = tuple(proxies)
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
        self._pace_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last: dict[str, float] = {}

    def policy_key(self, host: str) -> str:
        """The key pacing, cooldown and block state are shared under: an exact key, else the longest
        `.suffix` key the host ends with, else the host itself (which gets the default policy)."""
        if host in self._hosts:
            return host
        for suffix in self._suffixes:
            if host.endswith(suffix):
                return suffix
        return host

    def _policy(self, key: str) -> HostPolicy:
        return self._hosts.get(key, self._default)

    def _adaptive(self, key: str) -> tuple[AdaptivePace, list[float]] | None:
        pace = self._policy(key).pace
        if not isinstance(pace, AdaptivePace):
            return None
        with self._locks_guard:
            return pace, self._adaptive_state.setdefault(key, [pace.start, 0])

    def _request_transport(self, method: str, url: str, **kwargs):
        host = urlparse(url).hostname or ""
        if not self._policy(self.policy_key(host)).browser:
            return self._session.request(method, url, **kwargs)
        return self._playwright_session.request(method, url, **kwargs)

    def _host_lock(self, host: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def interval(self, host: str) -> float:
        """The current base interval of a host."""
        host = self.policy_key(host)
        if (adaptive := self._adaptive(host)) is not None:
            with self._host_lock(host):
                return adaptive[1][0]
        return self._policy(host).pace[0]

    def _record(self, host: str, clean: bool, *, immediate: bool = False,
                reason: str = "transient failure") -> None:
        """Apply response feedback, distinguishing blocks from transient failures.

        Failure streaks follow completion order across workers. A clean response
        resets the streak; each full streak can slow the host once.
        """
        adaptive = self._adaptive(host)
        if adaptive is None:
            return
        cfg, state = adaptive
        with self._host_lock(host):
            before = state[0]
            if not clean:
                state[1] = 0
                failures = self._failure_streaks.get(host, 0) + 1
                if immediate or failures >= cfg.failure_streak:
                    state[0] = min(cfg.ceiling, state[0] * cfg.slowdown)
                    self._failure_streaks[host] = 0
                    if not immediate:
                        reason = f"{failures} consecutive network/5xx failures; latest: {reason}"
                else:
                    self._failure_streaks[host] = failures
            else:
                self._failure_streaks[host] = 0
                state[1] += 1
                if state[1] >= cfg.clean_streak:
                    state[0], state[1] = max(cfg.floor, state[0] * cfg.speedup), 0
                reason = "clean responses"
            after = state[0]
        if after != before:
            log.info("%s: pacing base %.2fs -> %.2fs (%s)", host, before, after, reason)

    def _pace(self, host: str) -> None:
        # Serialize request starts without holding the adaptive-state lock during
        # sleeps. Completed responses must be able to record feedback and return.
        with self._locks_guard:
            gate = self._pace_locks.setdefault(host, threading.Lock())
        with gate:
            if (adaptive := self._adaptive(host)) is not None:
                with self._host_lock(host):
                    base = adaptive[1][0]
                jitter = 0.3 * base
            else:
                base, jitter = self._policy(host).pace
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
                # Rate-limit retry delays apply to every worker/session, even
                # when --wait-on-block is off. They are distinct from hard blocks.
                retry_wait = self._cooldown.remaining(f"rate:{host}", now=self._clock())
                if retry_wait > 0:
                    self._sleep(retry_wait)
                    continue
                return
            if not self._wait_on_block:
                raise Blocked(host, until, saved_cooldown=True)
            log.info("%s: waiting for cooldown until %s", host, _clock_time(until))
            self._sleep(remaining)

    def _block(self, host: str, retry_after: float) -> float:
        return self._cooldown.block(host, max(self._block_seconds, retry_after), now=self._clock())

    def request(self, method: str, url: str, *, retry_network: int = 0, **kwargs) -> requests.Response:
        host = self.policy_key(urlparse(url).hostname or "")
        policy = self._policy(host)
        kwargs.setdefault("allow_redirects", False)
        kwargs.setdefault("timeout", 30)
        attempt = 0
        block_waits = 0
        network_retries = 0
        while True:
            self._wait_or_raise(host)
            self._pace(host)
            self._wait_or_raise(host)  # another worker may have been blocked while we paced
            send = dict(kwargs)
            if self._proxies is not None:
                with self._proxy_lock:
                    p = next(self._proxies)
                send["proxies"] = {"http": p, "https": p}
            try:
                resp = self._transport(method, url, **send)
            except requests.RequestException as exc:
                transient = isinstance(exc, (requests.Timeout, requests.ConnectionError)) and not isinstance(
                    exc, requests.exceptions.SSLError)
                if transient:
                    self._record(host, clean=False, reason=type(exc).__name__)
                if method.upper() in {"GET", "HEAD"} and transient and network_retries < retry_network:
                    log.warning("%s: %s; retry %d/%d", host, type(exc).__name__, network_retries + 1, retry_network)
                    self._sleep(min(2 ** network_retries, 30))
                    network_retries += 1
                    continue
                raise

            # stream=True callers (the host check) never download the body, so only the URL tells a block
            read_body = not kwargs.get("stream")
            if (reason := policy.blocked(resp, read_body)) is not None:
                self._record(host, clean=False, immediate=True, reason="CAPTCHA/block response")
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After", ""), self._clock())
                until = self._block(host, retry_after)
                log.warning("%s sent a block page: %s", host, reason)
                log.debug("%s block response: %s", host, describe_response(resp, read_body))
                if self._wait_on_block and block_waits < self._max_retries:
                    block_waits += 1
                    continue
                raise Blocked(host, until)

            self._record(host, clean=resp.status_code != 429 and resp.status_code < 500,
                         immediate=resp.status_code == 429, reason=f"HTTP {resp.status_code}")
            if resp.status_code == 429:
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After", ""), self._clock())
                if attempt >= self._max_retries:
                    log.warning("%s still rate-limits after %d backoffs (%s)", host, attempt, describe_response(resp))
                    raise Blocked(host, self._block(host, retry_after))
                backoff = max(min(60 * 2**attempt, 900), retry_after)
                self._cooldown.block(f"rate:{host}", backoff, now=self._clock())
                log.warning("%s: HTTP 429; pausing all requests to this host for at least %.0fs", host, backoff)
                self._sleep(backoff)
                attempt += 1
                continue
            return resp

    def block(self, host: str, reason: str) -> Blocked | None:
        """Put a host in cooldown for a block that no single response shows (e.g. a run of refused requests that
        each look like an ordinary error), as a block response would: the host slows down and the cooldown is
        saved. Returns the `Blocked` for the caller to raise, or None with `wait_on_block`: the next request to
        the host then waits the cooldown out."""
        host = self.policy_key(host)
        self._record(host, clean=False, immediate=True, reason=reason)
        until = self._block(host, 0.0)
        log.warning("%s: %s; treated as a block", host, reason)
        return None if self._wait_on_block else Blocked(host, until)

    def derive(self, *, wait_on_block: bool | None = None, max_retries: int | None = None) -> PacedSession:
        """A new session sharing this one's cooldown, host policies, proxies, transport, sleep and clock,
        with its own block/retry policy and its own pacing state. Close it separately."""
        return PacedSession(
            self._cooldown, self._hosts,
            default=self._default, proxies=self._proxy_list,
            wait_on_block=self._wait_on_block if wait_on_block is None else wait_on_block,
            max_retries=self._max_retries if max_retries is None else max_retries,
            block_seconds=self._block_seconds, transport=self._custom_transport,
            sleep=self._sleep, clock=self._clock, rng=self._rng,
            solve_captcha=self._solve_captcha, cookie_dir=self._cookie_dir,
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._session.close()
        if self._playwright_session is not None:
            self._playwright_session.close()


def get_ok(session: PacedSession, url: str, *, retry_network: int = 0, shown_url: str | None = None) -> requests.Response:
    """GET following redirects; raise `requests.HTTPError("HTTP <status> for <url>")` unless the status is 200.
    `shown_url` replaces the URL in that message (e.g. without an expiring signature)."""
    resp = session.get(url, allow_redirects=True, retry_network=retry_network)
    if resp.status_code != 200:
        raise requests.HTTPError(f"HTTP {resp.status_code} for {shown_url or url}", response=resp)
    return resp


def run_session(out: Path, hosts: Mapping[str, HostPolicy], *, default: HostPolicy = HostPolicy(),
                proxies: Sequence[str] = (), wait_on_block: bool = False, solve_captcha: bool = False) -> PacedSession:
    """A session for a tool's run: cooldown and CAPTCHA cookies live under the run's cache dir."""
    return PacedSession(Cooldown(cache_dir(out) / "cooldown.sqlite3"), hosts, default=default, proxies=proxies,
                        wait_on_block=wait_on_block, solve_captcha=solve_captcha,
                        cookie_dir=cache_dir(out) / "captcha")


def _dns_failure(e: BaseException) -> bool:
    """Whether a requests error was caused by a host name that does not resolve."""
    stack, seen = [e], set()
    while stack:
        x = stack.pop()
        if not isinstance(x, BaseException) or id(x) in seen:
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

    def __init__(self, session: PacedSession, cache: ExtractionCache | None = None, timeout: float = 5.0):
        self._s = session
        self._cache = cache
        self._timeout = timeout
        self._candidates: set[str] = set()
        self._known: dict[str, bool] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._reported: set[str] = set()
        self._lock = threading.Lock()

    def remember(self, host: str) -> None:
        host = host.strip().lower().removeprefix("www.")
        if "." in host and not any(c in host for c in "/ :"):
            with self._lock:
                self._candidates.add(host)

    def resolve(self, host: str) -> str:
        """Repair only a DNS failure with exactly one one-edit known-domain match."""
        if self._exists(host) or self._exists(host[4:] if host.startswith("www.") else "www." + host):
            self.remember(host)
            return host
        from .domains import one_edit_apart
        bare = host.lower().removeprefix("www.")
        with self._lock:
            matches = [c for c in self._candidates if one_edit_apart(bare, c)]
        if len(matches) == 1 and self(matches[0]):
            corrected = ("www." if host.startswith("www.") else "") + matches[0]
            log.info("OCR domain corrected: %s -> %s (known domain)", host, corrected)
            return corrected
        self(host)  # retain the existing warning for unresolved names
        return ""

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
        if first:
            log.warning("%s does not resolve (DNS); treated as misread by OCR", host)
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
