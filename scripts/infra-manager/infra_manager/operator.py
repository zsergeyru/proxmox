"""Операторские команды, выполняемые внутри управляющего гостя."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

from .common import InfraManagerError, console, run
from .guest_deploy import project_branch_for_checkout
from .guest_operations import list_local_guests, run_local_guest_status
from .pve import PveClient
from .pve_host import (
    preflight_recovery_contour,
    repair_openbao_on_host,
    show_openbao_operator_credentials,
)
from .semaphore import run_operator_guest_task
from .settings import PATHS

ACTIVATE_RUNTIME = Path("/usr/local/sbin/infra-manager-activate-runtime")
COMPOSE_FILE = PATHS.compose_dir / "docker-compose.yml"
VERSIONS_FILE = PATHS.compose_dir / ".versions.env"


def _require_root() -> None:
    if os.geteuid() != 0:
        raise InfraManagerError(
            "операторская команда infra-manager должна выполняться от root"
        )


def _pve_node() -> str:
    explicit = os.environ.get("INFRA_PVE_NODE", "").strip()
    if explicit:
        if any(character.isspace() for character in explicit) or "/" in explicit:
            raise InfraManagerError("INFRA_PVE_NODE имеет некорректный формат")
        return explicit

    client = PveClient()
    node = urlparse(client.url).hostname
    if not node:
        raise InfraManagerError(
            f"Не удалось определить узел PVE из {client.url!r}"
        )
    return node


def operator_status(*, show_secrets: bool) -> int:
    """Проверить управляющий контур изнутри управляющего гостя."""

    from .status import check_status

    result = check_status(full=True, quiet=False)
    if result == 0 and show_secrets:
        print("\n==> OpenBao UI")
        show_openbao_operator_credentials(_pve_node())
    return result


def operator_guest_status(
    vmid: int,
    *,
    show_secrets: bool,
) -> int:
    """Показать единый статус выбранного гостя."""

    return run_local_guest_status(
        PATHS.repo_root,
        vmid,
        show_secrets=show_secrets,
    )


def operator_guest_task(
    operation: str,
    vmid: int,
    *,
    show_secrets: bool,
) -> int:
    """Выполнить штатную операцию напрямую внутри infra-manager."""

    result = run_guest_operation(
        PATHS.repo_root,
        operation,
        vmid,
        show_secrets=show_secrets,
    )
    if result != 0 or operation in {"deploy", "test"}:
        return result
    return operator_guest_status(
        vmid,
        show_secrets=show_secrets,
    )


def operator_repair(*, show_secrets: bool) -> int:
    """Безопасно восстановить службы управляющего гостя."""

    node = _pve_node()

    console.info("Проверка сохранности постоянного состояния")
    preflight_recovery_contour(node)

    console.info("Запуск Docker")
    run(["systemctl", "start", "docker"])

    for required in (COMPOSE_FILE, VERSIONS_FILE):
        if not required.is_file() or required.stat().st_size == 0:
            raise InfraManagerError(
                f"Отсутствует {required}; используйте infra-manager recover на PVE"
            )

    console.info("Запуск OpenBao")
    run(
        [
            "docker",
            "compose",
            "--env-file",
            str(VERSIONS_FILE),
            "-f",
            str(COMPOSE_FILE),
            "up",
            "-d",
            "openbao",
        ]
    )

    console.info("Безопасное восстановление OpenBao через PVE-only контур")
    repair_openbao_on_host(node)

    if not ACTIVATE_RUNTIME.is_file():
        raise InfraManagerError(
            f"Отсутствует {ACTIVATE_RUNTIME}; используйте infra-manager recover на PVE"
        )

    console.info("Активация управляющей среды")
    environment = os.environ.copy()
    environment["INFRA_PROJECT_BRANCH"] = project_branch_for_checkout(
        PATHS.repo_root
    )
    environment["INFRA_PVE_NODE"] = node
    run([str(ACTIVATE_RUNTIME)], env=environment)

    return operator_status(show_secrets=show_secrets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-manager",
        description="Управление инфраструктурой через управляющий гость.",
    )
    parser.add_argument(
        "--trusted-pve",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "guests",
        help="показать поддерживаемых гостей и их состояние",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="проверить управляющий контур или выбранного гостя",
    )
    status_parser.add_argument(
        "vmid",
        nargs="?",
        type=int,
        help="VMID гостя; без VMID проверяется управляющий контур",
    )

    for command, help_text in (
        ("deploy", "развернуть или обновить гостя"),
        ("sync", "синхронизировать гостя"),
        ("test", "проверить гостя без изменений"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("vmid", type=int, help="VMID гостя")

    repair_parser = subparsers.add_parser(
        "repair",
        help="исправить управляющий контур или выбранного гостя",
    )
    repair_parser.add_argument(
        "vmid",
        nargs="?",
        type=int,
        help="VMID гостя; без VMID исправляется управляющий контур",
    )

    subparsers.add_parser(
        "recover",
        help="аварийное восстановление выполняется оболочкой на PVE",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _require_root()
        show_secrets = bool(args.trusted_pve)

        if args.command == "guests":
            return list_local_guests(PATHS.repo_root)
        if args.command == "status":
            if args.vmid is None:
                return operator_status(show_secrets=show_secrets)
            return operator_guest_status(
                args.vmid,
                show_secrets=show_secrets,
            )
        if args.command == "repair":
            if args.vmid is None:
                return operator_repair(show_secrets=show_secrets)
            return operator_guest_task(
                "repair",
                args.vmid,
                show_secrets=show_secrets,
            )
        if args.command in {"deploy", "sync", "test"}:
            return operator_guest_task(
                args.command,
                args.vmid,
                show_secrets=show_secrets,
            )
        if args.command == "recover":
            raise InfraManagerError(
                "infra-manager recover разрешён только на физическом PVE"
            )
        raise InfraManagerError(f"Неизвестная команда: {args.command}")
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
