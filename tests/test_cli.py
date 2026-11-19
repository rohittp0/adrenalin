from argparse import Namespace

import pytest

import adrenalin.cli as cli
from adrenalin.cli import discover_tools, main
from adrenalin.common.tool import Tool


def test_discovers_tool_modules_exposing_TOOL(tmp_path, monkeypatch):
    pkg = tmp_path / "fake_tools"; (pkg / "hello").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "hello" / "__init__.py").write_text(
        "from adrenalin.common.tool import Tool\n"
        "TOOL = Tool('hello', 'says hi', lambda p: p.add_argument('who'), lambda a: print('hi', a.who) or 0)\n")
    (pkg / "not_a_tool.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import fake_tools
    assert set(discover_tools(fake_tools)) == {"hello"}


def test_real_package_discovers_ads_transparency():
    assert "ads-transparency" in discover_tools()


def _hello(code):
    def run(a: Namespace) -> int:
        print("hi", a.who)
        return code
    return Tool("hello", "says hi", lambda p: p.add_argument("who"), run)


def test_main_dispatches_and_returns_tool_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_tools", lambda: {"hello": _hello(3)})
    assert main(["hello", "bob"]) == 3
    assert "hi bob" in capsys.readouterr().out


def test_main_without_tool_prints_tool_list_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_tools", lambda: {"hello": _hello(0)})
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2
    assert "hello" in capsys.readouterr().err
