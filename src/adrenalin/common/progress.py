"""tqdm progress bars for a staged WorkQueue run, refreshed from the queue by a background thread."""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TextIO

from tqdm import tqdm

from adrenalin.common.pipeline import WorkQueue


@dataclass(frozen=True)
class Bar:
    label: str
    # how many items are finished (done or failed) and how many there are (None: not known yet)
    count: Callable[[dict[tuple[str, str], int]], tuple[int, int | None]]
    unit: str = "it"


def stage_bar(stage: str, label: str | None = None, unit: str = "it",
              total: Callable[[dict[tuple[str, str], int]], int | None] | None = None) -> Bar:
    """A bar for one stage: finished items over the items queued so far (or `total`, when given)."""
    def count(c: dict[tuple[str, str], int]) -> tuple[int, int | None]:
        finished = c.get((stage, "done"), 0) + c.get((stage, "failed"), 0)
        queued = sum(n for (s, _), n in c.items() if s == stage)
        return finished, total(c) if total is not None else queued
    return Bar(label or stage, count, unit)


class QueueProgress:
    """Draws one tqdm bar per `Bar` while a pipeline runs. Use `write` as the run's log function: messages are
    printed above the bars instead of breaking them. Rates and ETAs count only work done in this run."""

    def __init__(self, bars: Sequence[Bar], *, interval: float = 0.5, file: TextIO | None = None):
        self.bars = list(bars)
        self.interval = interval
        self.file = file or sys.stderr
        self._tqdm: list[tqdm] = []
        self._queue: WorkQueue | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._failed: list[int] = []

    def write(self, message: str) -> None:
        with self._lock:
            tqdm.write(message, file=self.file)

    def start(self, queue: WorkQueue) -> QueueProgress:
        self._queue = queue
        counts = queue.counts()
        for i, b in enumerate(self.bars):
            n, total = b.count(counts)
            self._tqdm.append(tqdm(total=total, initial=n, desc=b.label, unit=b.unit, position=i, leave=True,
                                   file=self.file, dynamic_ncols=True, smoothing=0.05))
        self._thread = threading.Thread(target=self._loop, name="progress", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.refresh()

    def refresh(self) -> None:
        if self._queue is None:
            return
        counts = self._queue.counts()
        with self._lock:
            for b, bar in zip(self.bars, self._tqdm):
                n, total = b.count(counts)
                if total is not None and total < n:
                    total = n
                if bar.total != total:
                    bar.total = total
                if n > bar.n:
                    bar.update(n - bar.n)  # update() keeps tqdm's rate and ETA estimates
                else:
                    bar.refresh()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.refresh()
        with self._lock:
            for bar in self._tqdm:
                bar.close()
            self._tqdm = []

    def __enter__(self) -> QueueProgress:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@contextmanager
def run_progress(q: WorkQueue, bars: Sequence[Bar], enabled: bool) -> Iterator[QueueProgress | None]:
    """Start `QueueProgress` on `q` for the block and close it afterwards; yields None when disabled."""
    if not enabled:
        yield None
        return
    progress = QueueProgress(bars).start(q)
    try:
        yield progress
    finally:
        progress.close()
