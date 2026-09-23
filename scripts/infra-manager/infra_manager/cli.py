"""Командная строка Python-части infra-manager."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .common import InfraManagerError, console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-manager",
        description="Управление служебным LXC 910 infra-manager.",
    )
    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser(
        "setup",
        help="подготовить и обновить LXC 910",
        description=(
            "Подготовить Debian, Docker, infra-runtime и сохранить текущий "
            "внешний bootstrap-контракт."
        ),
    )
    setup_parser.set_defaults(handler="setup")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
        if args.command is None:
            parser.print_help()
            return 0

        if args.handler == "setup":
            from .setup import run_setup

            return run_setup()

        raise InfraManagerError(f"Неизвестная команда: {args.command}")
    except InfraManagerError as exc:
        console.error(str(exc))
        return 1
