"""Командная строка Python-части infra-manager."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .common import InfraManagerError, console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-manager",
        description="Управление служебным LXC infra-manager.",
    )
    subparsers = parser.add_subparsers(dest="command")

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
        help="проверить полный контракт и показать подробное состояние",
    )
    status_parser.add_argument(
        "--quiet",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    status_parser.set_defaults(handler="status")

    access_parser = subparsers.add_parser(
        "pve-access-check",
        help="проверить права PVE API token infra-manager",
    )
    access_parser.set_defaults(handler="pve-access-check")

    lifecycle_parser = subparsers.add_parser(
        "pve-lifecycle-test",
        help="выполнить интеграционный тест жизненного цикла LXC через PVE API",
    )
    lifecycle_parser.add_argument(
        "--apply",
        action="store_true",
        help="разрешить создание и удаление временного LXC 9098",
    )
    lifecycle_parser.set_defaults(handler="pve-lifecycle-test")

    recovery_prepare = subparsers.add_parser(
        "recovery-prepare",
        help="подготовить PVE-only аварийный контур infra-manager",
    )
    recovery_prepare.add_argument(
        "--repo-root",
        type=str,
        required=True,
        help="корень рабочей копии проекта",
    )
    recovery_prepare.set_defaults(handler="recovery-prepare")

    recovery_check = subparsers.add_parser(
        "recovery-check",
        help="проверить полный аварийный контур infra-manager",
    )
    recovery_check.set_defaults(handler="recovery-check")

    portal_bookmarks = subparsers.add_parser(
        "portal-bookmarks",
        help="сформировать кнопки Homepage для операций гостя",
    )
    portal_bookmarks.add_argument(
        "--guest-vmid",
        type=int,
        required=True,
        help="VMID гостя",
    )
    portal_bookmarks.add_argument(
        "--output",
        type=str,
        help="записать bookmarks.yaml в файл вместо stdout",
    )
    portal_bookmarks.set_defaults(handler="portal-bookmarks")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
        if args.command is None:
            parser.print_help()
            return 0

        if args.handler == "semaphore-project":
            from .semaphore import configure_project

            return configure_project()

        if args.handler == "status":
            from .status import check_status

            return check_status(full=args.full, quiet=args.quiet)

        if args.handler == "pve-access-check":
            from .pve import check_access

            return check_access()

        if args.handler == "pve-lifecycle-test":
            from .pve_lifecycle import run_lifecycle_test

            return run_lifecycle_test(apply=args.apply)

        if args.handler == "recovery-prepare":
            from pathlib import Path

            from .recovery import prepare_recovery

            return prepare_recovery(Path(args.repo_root).resolve())

        if args.handler == "recovery-check":
            from .recovery import check_recovery

            return check_recovery()

        if args.handler == "portal-bookmarks":
            from pathlib import Path

            from .portal import render_portal_bookmarks, write_portal_bookmarks

            if args.output:
                write_portal_bookmarks(args.guest_vmid, Path(args.output))
            else:
                print(render_portal_bookmarks(args.guest_vmid), end="")
            return 0

        raise InfraManagerError(f"Неизвестная команда: {args.command}")
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1
