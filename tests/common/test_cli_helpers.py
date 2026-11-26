import argparse
import logging
from datetime import date

import pytest

from adrenalin.common import cli
from adrenalin.common.http import Blocked


def parser():
    p = argparse.ArgumentParser()
    cli.add_run_arguments(p)
    cli.add_period_arguments(p)
    return p


@pytest.mark.parametrize("failed,errors,blocked,incomplete,expected", [
    ({}, [], None, False, 0),
    ({"fetch": 1}, [], None, False, 1),
    ({}, ["boom"], None, False, 1),
    ({}, [], None, True, 3),
    ({"fetch": 1}, [], None, True, 1),
    ({}, [], 123.0, True, 2),
    ({"fetch": 1}, ["x"], 123.0, False, 2),
])
def test_exit_code_precedence(failed, errors, blocked, incomplete, expected):
    assert cli.exit_code(failed=failed, errors=errors, blocked_until=blocked, incomplete=incomplete) == expected


def test_days_excludes_start():
    p = parser()
    with pytest.raises(SystemExit) as e:
        cli.resolve_period(p.parse_args(["--days", "7", "--start", "2026-11-01"]), p)
    assert e.value.code == 2


def test_days_resolves_to_window_ending_today():
    p = parser()
    start, end = cli.resolve_period(p.parse_args(["--days", "3"]), p)
    assert end == date.today() and (end - start).days == 2


def test_region_validated():
    p = parser()
    with pytest.raises(SystemExit) as e:
        cli.resolve_period(p.parse_args(["--region", "ZZZ"]), p)
    assert e.value.code == 2


def test_run_arguments_defaults():
    a = parser().parse_args([])
    assert (str(a.out), a.proxies, a.wait_on_block, a.solve_captcha, a.progress, a.verbose) == ("out", [], False, False, False, False)


def test_finish_logs_lines_and_returns_code(capsys):
    with cli.console_logging():
        code = cli.finish(failed={"fetch": 2}, errors=["e"], blocked_until=None,
                          incomplete_message="INCOMPLETE: x", pending_messages=("pending y",))
    assert code == 1
    assert capsys.readouterr().err.splitlines() == [
        "failed items (fetch: 2, errors: 1); rerun to retry", "pending y", "INCOMPLETE: x"]


def test_run_cli_maps_exceptions(capsys):
    args = argparse.Namespace(verbose=False)

    def blocked():
        raise Blocked("h", 0.0)

    class Mine(Exception):
        pass

    def mine():
        raise Mine("nope")

    assert cli.run_cli(args, blocked) == 2
    assert "blocked until" in capsys.readouterr().err
    assert cli.run_cli(args, mine, user_errors=(Mine,)) == 1
    assert capsys.readouterr().err.strip() == "error: nope"
    assert cli.run_cli(args, lambda: 3) == 3
