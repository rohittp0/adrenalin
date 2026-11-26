import logging
import sys

from tqdm import tqdm

from adrenalin.common.runlog import LOGGER, console_logging, run_log


def test_console_writes_above_bars(monkeypatch):
    seen = []
    monkeypatch.setattr(tqdm, "write", lambda msg, file=None, **kw: seen.append((msg, file)))
    with console_logging():
        log = logging.getLogger("adrenalin.x")
        log.warning("w")
        log.info("i")
        log.debug("d")
        log.error("e")
    assert [m for m, _ in seen] == ["warning: w", "i", "error: e"]
    assert all(f is sys.stderr for _, f in seen)


def test_console_verbose_shows_debug_and_restores_logger(monkeypatch):
    seen = []
    monkeypatch.setattr(tqdm, "write", lambda msg, file=None, **kw: seen.append(msg))
    lg = logging.getLogger(LOGGER)
    before = (lg.level, lg.propagate)
    with console_logging(verbose=True):
        assert lg.propagate is False
        logging.getLogger("adrenalin.y").debug("d")
        with console_logging():
            assert len(lg.handlers) == 1
    assert seen == ["d"]
    assert lg.handlers == [] and (lg.level, lg.propagate) == before


def test_console_follows_swapped_stderr(capsys):
    with console_logging():
        logging.getLogger("adrenalin.x").warning("hi")
    assert capsys.readouterr().err == "warning: hi\n"


def test_run_log_captures_debug_and_detaches(tmp_path):
    lg = logging.getLogger(LOGGER)
    before = lg.level
    with run_log(tmp_path) as path:
        logging.getLogger("adrenalin.x").debug("dbg line")
    assert path == tmp_path / "run.log"
    text = path.read_text()
    assert "DEBUG adrenalin.x: dbg line" in text
    assert lg.handlers == [] and lg.level == before


def test_run_log_works_beside_console(tmp_path, monkeypatch):
    monkeypatch.setattr(tqdm, "write", lambda *a, **k: None)
    with console_logging(), run_log(tmp_path) as path:
        logging.getLogger("adrenalin.x").debug("dbg line")
    assert "dbg line" in path.read_text()


def test_root_logger_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(tqdm, "write", lambda *a, **k: None)
    root = logging.getLogger()
    before = list(root.handlers)
    with console_logging(), run_log(tmp_path):
        logging.getLogger("adrenalin").info("x")
    assert root.handlers == before


def test_nested_run_logs_each_get_own_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    log = logging.getLogger("adrenalin.x")
    with run_log(a):
        with run_log(b):
            log.info("inner")
        log.info("outer")
    assert "inner" in (a / "run.log").read_text() and "inner" in (b / "run.log").read_text()
    assert "outer" in (a / "run.log").read_text() and "outer" not in (b / "run.log").read_text()


def test_run_log_leaves_root_handlers_at_their_level(tmp_path):
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    root, lg = logging.getLogger(), logging.getLogger(LOGGER)
    handler, saved = Collect(), root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)  # a library caller's basicConfig(level=WARNING)
    try:
        assert lg.level == logging.NOTSET and lg.propagate
        with run_log(tmp_path) as path:
            log = logging.getLogger("adrenalin.x")
            log.debug("d")
            log.info("i")
            log.warning("w")
            log.error("e")
        assert seen == ["w", "e"]
        assert [line.split(": ", 1)[1] for line in path.read_text().splitlines()] == ["d", "i", "w", "e"]
        assert lg.level == logging.NOTSET and lg.propagate and len(lg.handlers) == 0
    finally:
        root.removeHandler(handler)
        root.setLevel(saved)


def test_run_log_prints_nothing_without_logging_configured(tmp_path, capsys):
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()  # a library caller that configured no logging (pytest puts its own handlers here)
    try:
        with run_log(tmp_path) as path:
            logging.getLogger("adrenalin.x").warning("w")
    finally:
        root.handlers[:] = saved
    assert capsys.readouterr().err == ""  # no `logging.lastResort` line
    assert path.read_text().rstrip().endswith("w")
