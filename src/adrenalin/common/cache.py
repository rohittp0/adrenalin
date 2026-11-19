from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class ExtractionCache:
    """SQLite-backed cache of extracted fields, keyed by (key, method, model). Callers namespace keys
    (`simgad:<id>`, `domain:<host>`) because tools and kinds of data share one file."""

    def __init__(self, path: Path, timeout: float = 30.0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Shared by every run under out/: concurrent runs wait for a lock instead of failing after 5 s.
        self.timeout = timeout
        self._db = sqlite3.connect(str(path), timeout=timeout, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS extractions ("
                "key TEXT NOT NULL, method TEXT NOT NULL, model TEXT NOT NULL, "
                "fields TEXT NOT NULL, PRIMARY KEY (key, method, model))"
            )

    def get(self, key: str, method: str, model: str = "") -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fields FROM extractions WHERE key=? AND method=? AND model=?",
                (key, method, model),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, method: str, model: str, fields: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO extractions (key, method, model, fields) VALUES (?,?,?,?)",
                (key, method, model, json.dumps(fields)),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
