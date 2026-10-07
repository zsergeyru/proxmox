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

    guest_list_parser = subparsers.add_parser(
        "guest-list",
        help="показать поддерживаемых гостей проекта",
    )
    guest_list_parser.set_defaults(handler="guest-list")

    guest_task_parser = subparsers.add_parser(
        "guest-task",
        help="создать штатное задание Semaphore для гостя",
    )
    guest_task_parser.add_argument(
        "--operation",
        required=True,
        choices=("deploy", "repair", "test", "sync"),
        help="операция над гостем",
    )
    guest_task_parser.add_argument(
        "--guest-vmid",
        type=int,
        required=True,
        help="VMID гостя",
    )
    guest_task_parser.set_defaults(handler="guest-task")

    guest_status_parser = subparsers.add_parser(
        "guest-status",
        help="показать общий статус выбранного гостя",
    )
    guest_status_parser.add_argument(
        "--guest-vmid",
        type=int,
        required=True,
        help="VMID гостя",
    )
    guest_status_parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="явно разрешить вывод операторских паролей",
    )
    guest_status_parser.set_defaults(handler="guest-status")

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
        help="VMID гостя; без значения используется роль infra-manager",
    )
    portal_bookmarks.add_argument(
        "--output",
        type=str,
        help="записать bookmarks.yaml в файл вместо stdout",
    )
    portal_bookmarks.set_defaults(handler="portal-bookmarks")

    portal_services = subparsers.add_parser(
        "portal-services",
        help="сформировать services.yaml Homepage для гостя",
    )
    portal_services.add_argument(
        "--guest-vmid",
        type=int,
        required=True,
        help="VMID гостя",
    )
    portal_services.add_argument(
        "--output",
        type=str,
        help="записать services.yaml в файл вместо stdout",
    )
    portal_services.set_defaults(handler="portal-services")

    portal_gateway = subparsers.add_parser(
        "portal-gateway",
        help="запустить защищённый переход к операциям Semaphore",
    )
    portal_gateway.add_argument(
        "--bind",
        default="0.0.0.0",
        help="адрес прослушивания",
    )
    portal_gateway.add_argument(
        "--port",
        type=int,
        help="TCP-порт; по умолчанию берётся из общих настроек",
    )
    portal_gateway.set_defaults(handler="portal-gateway")
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

        if args.handler == "guest-list":
            from .guest_operations import list_local_guests
            from .settings import PATHS

            return list_local_guests(PATHS.repo_root)

        if args.handler == "guest-task":
            from .semaphore import run_operator_guest_task

            run_operator_guest_task(
                args.operation,
                args.guest_vmid,
            )
            return 0

        if args.handler == "guest-status":
            from .guest_operations import run_local_guest_status
            from .settings import PATHS

            return run_local_guest_status(
                PATHS.repo_root,
                args.guest_vmid,
                show_secrets=args.show_secrets,
            )

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

            from .guest_catalog import find_guest_by_role
            from .portal import render_portal_bookmarks, write_portal_bookmarks
            from .settings import PATHS, SETTINGS

            vmid = args.guest_vmid
            if vmid is None:
                vmid = find_guest_by_role(
                    PATHS.repo_root,
                    SETTINGS.infra_manager_role,
                ).vmid

            if args.output:
                write_portal_bookmarks(vmid, Path(args.output))
            else:
                print(render_portal_bookmarks(vmid), end="")
            return 0

        if args.handler == "portal-services":
            from pathlib import Path

            from .portal import render_portal_services, write_portal_services

            if args.output:
                write_portal_services(args.guest_vmid, Path(args.output))
            else:
                print(render_portal_services(args.guest_vmid), end="")
            return 0

        if args.handler == "portal-gateway":
            from .portal_gateway import run_portal_gateway
            from .settings import SETTINGS

            port = (
                SETTINGS.portal_gateway_port
                if args.port is None
                else args.port
            )
            if not (0 < port < 65536):
                raise InfraManagerError(
                    "Порт посредника должен быть от 1 до 65535"
                )
            return run_portal_gateway(args.bind, port)

        raise InfraManagerError(f"Неизвестная команда: {args.command}")
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1
