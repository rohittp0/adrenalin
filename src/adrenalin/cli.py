import argparse
import importlib
import pkgutil
import sys
from types import ModuleType

import adrenalin.tools
from adrenalin.common.tool import Tool


def discover_tools(package: ModuleType = adrenalin.tools) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        tool = getattr(module, "TOOL", None)
        if isinstance(tool, Tool):
            tools[tool.name] = tool
    return tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adrenalin", description="Competitor-analysis tools for marketing teams")
    sub = parser.add_subparsers(dest="tool", title="tools")
    tools = discover_tools()
    for name in sorted(tools):
        tool = tools[name]
        tool.add_arguments(sub.add_parser(name, help=tool.help, description=tool.help))
    args = parser.parse_args(argv)
    if args.tool is None:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return tools[args.tool].run(args)
