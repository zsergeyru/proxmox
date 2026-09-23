"""Командная строка Python-части infra-manager."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .common import InfraManagerError, console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-manager",
        description=(
            "Python-основа infra-manager. Рабочие команды 910 пока остаются "
            "в существующих shell-скриптах."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()

    try:
        parser.parse_args(argv)
    except InfraManagerError as exc:
        console.error(str(exc))
        return 1

    parser.print_help()
    return 0
