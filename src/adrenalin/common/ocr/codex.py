from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

DEFAULT_VISION_MODEL = os.environ.get("ADRENALIN_VISION_MODEL", "gpt-5.6-luna")


class CodexError(RuntimeError):
    """The codex CLI failed, timed out or is not installed."""


class CodexMissing(CodexError):
    """The codex binary is not installed. Raised for every later call too, without trying again."""


class CodexRunner:
    def __init__(
        self,
        model: str = DEFAULT_VISION_MODEL,
        limit: int = 200,
        concurrency: int = 2,
        timeout: float = 300,
        run=subprocess.run,
    ):
        self.model = model
        self.limit = limit
        self.timeout = timeout
        self._run = run
        self._sem = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()
        self.calls = 0
        self.skipped = 0
        self.missing = False  # circuit breaker: set once the binary is found missing

    def extract(self, image: Path, schema: dict, prompt: str) -> dict | None:
        with self._lock:
            if self.missing:
                raise CodexMissing("codex binary not found")
            if self.calls >= self.limit:
                self.skipped += 1
                return None
            self.calls += 1
        with self._sem, tempfile.TemporaryDirectory(prefix="adrenalin-codex-") as tmp:
            tmpdir = Path(tmp)
            schema_file = tmpdir / "schema.json"
            out_file = tmpdir / "out.json"
            schema_file.write_text(json.dumps(schema))
            argv = [
                "codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                "-m", self.model, "-c", "model_reasoning_effort=low",
                "-i", str(Path(image).resolve()),
                "--output-schema", str(schema_file), "-o", str(out_file),
                "--", prompt,
            ]
            try:
                proc = self._run(
                    argv, cwd=tmp, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=self.timeout,
                )
            except FileNotFoundError as e:
                with self._lock:
                    self.missing = True
                    self.calls -= 1  # nothing ran: don't use up the cap
                raise CodexMissing(f"codex binary not found: {e}") from e
            except subprocess.TimeoutExpired as e:
                raise CodexError(f"codex timed out after {self.timeout}s") from e
            if proc.returncode != 0:
                raise CodexError(f"codex exited {proc.returncode}: {(proc.stderr or '').strip()}")
            try:
                return json.loads(out_file.read_text())
            except (OSError, ValueError) as e:
                raise CodexError(f"codex produced no valid JSON output: {e}") from e
