"""SQLite-backed work queue and a staged, backpressured thread pipeline (ADR 0001)."""
import json
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from adrenalin.common.http import Blocked


def print_to_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Item:
    stage: str
    key: str
    payload: dict
    result: dict | None
    status: str
    error: str | None


_COLS = "stage, key, payload, result, status, error"


def _item(row) -> Item:
    return Item(row[0], row[1], json.loads(row[2]), json.loads(row[3]) if row[3] is not None else None,
                row[4], row[5])


class WorkQueue:
    def __init__(self, path: Path, *, max_attempts: int = 3):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL, key TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(stage, key));
            CREATE INDEX IF NOT EXISTS items_stage_status ON items(stage, status);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        with self._lock, self._db:
            self._db.execute("UPDATE items SET status='pending' WHERE status='running'")
            self._db.execute("UPDATE items SET status='pending' WHERE status='failed' AND attempts < ?",
                             (max_attempts,))

    def put(self, stage: str, key: str, payload: dict, *, update_payload: bool = False) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("INSERT OR IGNORE INTO items (stage, key, payload) VALUES (?, ?, ?)",
                                   (stage, key, json.dumps(payload)))
            if cur.rowcount == 1:
                return True
            if update_payload:
                self._db.execute("UPDATE items SET payload=? WHERE stage=? AND key=?",
                                 (json.dumps(payload), stage, key))
            return False

    def claim(self, stage: str) -> Item | None:
        with self._lock, self._db:
            # Find the id through the (stage, status) index first, then read only that row.
            found = self._db.execute(
                "SELECT id FROM items WHERE stage=? AND status='pending' ORDER BY id LIMIT 1", (stage,)).fetchone()
            if found is None:
                return None
            row = self._db.execute(f"SELECT {_COLS} FROM items WHERE id=?", found).fetchone()
            self._db.execute("UPDATE items SET status='running', attempts=attempts+1 WHERE id=?", found)
            item = _item(row)
            item.status = "running"
            return item

    def complete(self, stage: str, key: str, result: dict | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE items SET status='done', result=?, error=NULL WHERE stage=? AND key=?",
                             (json.dumps(result) if result is not None else None, stage, key))

    def fail(self, stage: str, key: str, error: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE items SET status='failed', error=? WHERE stage=? AND key=?",
                             (error, stage, key))

    def release(self, stage: str, key: str) -> None:
        """Return a claimed item to pending without counting the attempt."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE items SET status='pending', attempts=MAX(attempts-1, 0) "
                "WHERE stage=? AND key=? AND status='running'", (stage, key))

    def requeue(self, stage: str, keys: Iterable[str]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE items SET status='pending', result=NULL, error=NULL, attempts=0 "
                "WHERE stage=? AND key=?",
                [(stage, k) for k in keys])

    def backlog(self, *stages: str) -> int:
        if not stages:
            return 0
        marks = ",".join("?" * len(stages))
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM items WHERE stage IN ({marks}) AND status IN ('pending','running')",
                stages).fetchone()[0]

    def count(self, stage: str, status: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM items WHERE stage=? AND status=?",
                                    (stage, status)).fetchone()[0]

    def results(self, stage: str, status: str = "done") -> list[tuple[str, dict | None]]:
        """(key, result) pairs without loading payloads."""
        with self._lock:
            rows = self._db.execute("SELECT key, result FROM items WHERE stage=? AND status=? ORDER BY id",
                                    (stage, status)).fetchall()
        return [(k, json.loads(r) if r is not None else None) for k, r in rows]

    def items(self, stage: str, status: str | None = None) -> list[Item]:
        sql, args = f"SELECT {_COLS} FROM items WHERE stage=?", [stage]
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        with self._lock:
            return [_item(r) for r in self._db.execute(sql + " ORDER BY id", args).fetchall()]

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class Stage:
    name: str
    worker: Callable[[Item, WorkQueue], dict | None]
    concurrency: int = 1
    throttle_on: tuple[str, ...] = ()
    max_ahead: int = 0
    # called after the item is marked done; errors are logged and never fail the item
    on_done: Callable[[Item, dict | None], None] | None = None


@dataclass
class RunResult:
    blocked: Blocked | None = None
    # all failed items in the queue, including ones exhausted in earlier runs
    failed: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, queue: WorkQueue, stages: Sequence[Stage], *, poll: float = 0.2,
                 log: Callable[[str], None] = print_to_stderr):
        names = [s.name for s in stages]
        if len(set(names)) != len(names):
            raise ValueError(f"stage names must be unique: {names}")
        for s in stages:
            unknown = [n for n in s.throttle_on if n not in names]
            if unknown:
                raise ValueError(f"stage {s.name!r} throttles on unknown stage(s) {unknown}")
        self.queue, self.stages, self.poll, self.log = queue, list(stages), poll, log

    def run(self) -> RunResult:
        q, poll = self.queue, self.poll
        finished = {s.name: threading.Event() for s in self.stages}
        stopped = {s.name: threading.Event() for s in self.stages}
        live = {s.name: s.concurrency for s in self.stages}
        live_lock = threading.Lock()
        result = RunResult()
        state_lock = threading.Lock()
        upstream = {s.name: [p.name for p in self.stages[:i]] for i, s in enumerate(self.stages)}

        def throttled(stage: Stage) -> bool:
            live_targets = tuple(n for n in stage.throttle_on if not finished[n].is_set())
            return bool(live_targets) and q.backlog(*live_targets) > stage.max_ahead

        def loop(stage: Stage) -> None:
            name = stage.name
            while not stopped[name].is_set():
                if throttled(stage):
                    stopped[name].wait(poll)
                    continue
                item = q.claim(name)
                if item is None:
                    if all(finished[u].is_set() for u in upstream[name]) and q.backlog(name) == 0:
                        return
                    stopped[name].wait(poll)
                    continue
                try:
                    out = stage.worker(item, q)
                    q.complete(name, item.key, out)
                except Blocked as exc:
                    q.release(name, item.key)
                    with state_lock:
                        if result.blocked is None:
                            result.blocked = exc
                    self.log(f"{name}: blocked ({exc}); stopping stage")
                    stopped[name].set()
                    return
                except Exception as exc:  # noqa: BLE001 - any worker error fails only its item
                    msg = f"{type(exc).__name__}: {exc}"
                    q.fail(name, item.key, msg)
                    self.log(f"{name}: {item.key} failed: {msg}")
                else:
                    if stage.on_done is not None:
                        try:
                            stage.on_done(item, out)
                        except Exception as exc:  # noqa: BLE001 - post-completion hook must not fail the item
                            self.log(f"{name}: {item.key} on_done failed: {type(exc).__name__}: {exc}")

        def runner(stage: Stage) -> None:
            try:
                loop(stage)
            except Exception as exc:  # noqa: BLE001 - infrastructure failure, not a worker failure
                msg = f"{stage.name}: stage loop crashed: {type(exc).__name__}: {exc}"
                self.log(msg)
                with state_lock:
                    result.errors.append(msg)
                stopped[stage.name].set()
            finally:
                with live_lock:
                    live[stage.name] -= 1
                    last = live[stage.name] == 0
                if last:
                    finished[stage.name].set()

        threads = [threading.Thread(target=runner, args=(s,), name=f"{s.name}-{i}", daemon=True)
                   for s in self.stages for i in range(s.concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for s in self.stages:
            n = q.count(s.name, "failed")
            if n:
                result.failed[s.name] = n
        return result
