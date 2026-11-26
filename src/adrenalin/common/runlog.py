"""Logging for runs: a console handler that prints above tqdm bars, and a per-run `run.log` file.

Handlers attach to the `adrenalin` logger only, never to the root logger."""
from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tqdm import tqdm

LOGGER = "adrenalin"
_lock = threading.Lock()
_console: tuple[logging.Handler, int] | None = None  # the attached console handler and its nesting depth


class _ConsoleHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record), file=sys.stderr)  # sys.stderr looked up now, not at creation
        except Exception:
            self.handleError(record)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.levelno >= logging.ERROR:
            return f"error: {message}"
        if record.levelno >= logging.WARNING:
            return f"warning: {message}"
        return message


@contextmanager
def console_logging(verbose: bool = False) -> Iterator[None]:
    """Print INFO (DEBUG when verbose) and above from the `adrenalin` logger to stderr. Nested use shares one handler."""
    global _console
    logger = logging.getLogger(LOGGER)
    with _lock:
        if _console is not None:
            _console = (_console[0], _console[1] + 1)
            saved = None
        else:
            handler = _ConsoleHandler(logging.DEBUG if verbose else logging.INFO)
            handler.setFormatter(_ConsoleFormatter("%(message)s"))
            saved = (logger.level, logger.propagate)
            # DEBUG on the logger itself so a run.log beside the console still gets debug lines
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.addHandler(handler)
            _console = (handler, 1)
    try:
        yield
    finally:
        with _lock:
            handler, depth = _console
            if depth > 1:
                _console = (handler, depth - 1)
            else:
                logger.removeHandler(handler)
                if saved is not None:
                    logger.setLevel(saved[0])
                    logger.propagate = saved[1]
                _console = None


class _Propagate(logging.Handler):
    """Hands records on to the parent loggers' handlers, as propagation would, from `level` up."""

    def __init__(self, logger: logging.Logger, level: int):
        super().__init__(level)
        self._parent = logger.parent

    def emit(self, record: logging.LogRecord) -> None:
        if self._parent is not None:
            self._parent.callHandlers(record)


@contextmanager
def run_log(run_dir: Path) -> Iterator[Path]:
    """Append everything the `adrenalin` logger emits to `run_dir/run.log` for the duration.

    When the logger has no level of its own, it is lowered to DEBUG for the file, and the parent loggers' handlers
    (a library caller's `basicConfig`) still get only what they got before: records from the logger's previous
    effective level up."""
    logger = logging.getLogger(LOGGER)
    path = Path(run_dir) / "run.log"
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    lowered = logger.level == logging.NOTSET
    forward: logging.Handler | None = None
    propagate = logger.propagate
    if lowered:
        if propagate:
            forward = _Propagate(logger, logger.getEffectiveLevel())
            logger.addHandler(forward)
            logger.propagate = False
        logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield path
    finally:
        logger.removeHandler(handler)
        handler.close()
        if lowered:
            logger.setLevel(logging.NOTSET)
            if forward is not None:
                logger.removeHandler(forward)
                logger.propagate = propagate
