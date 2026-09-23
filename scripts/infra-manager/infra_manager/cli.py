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
            "Подготовить Debian, Docker, infra-runtime и привести "
            "infra-manager к заданному состоянию."
        ),
    )
    setup_parser.set_defaults(handler="setup")

    semaphore_parser = subparsers.add_parser(
        "semaphore-project",
        help="синхронизировать проект Semaphore",
    )
    semaphore_parser.set_defaults(handler="semaphore-project")

    status_parser = subparsers.add_parser(
        "status",
        help="проверить готовность infra-manager",
    )
    status_parser.add_argument(
        "--full",
        action="store_true",
        help="дополнительно проверить полный контракт PVE API",
    )
    status_parser.set_defaults(handler="status")

    access_parser = subparsers.add_parser(
        "pve-access-check",
        help="проверить права PVE API token infra-manager",
    )
    access_parser.set_defaults(handler="pve-access-check")
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

        if args.handler == "semaphore-project":
            from .semaphore import configure_project

            return configure_project()

        if args.handler == "status":
            from .status import check_status

            return check_status(full=args.full)

        if args.handler == "pve-access-check":
            from .pve import check_access

            return check_access()

        raise InfraManagerError(f"Неизвестная команда: {args.command}")
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1
