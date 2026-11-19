from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    help: str
    add_arguments: Callable[[ArgumentParser], None]
    run: Callable[[Namespace], int]
