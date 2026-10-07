"""Операторские команды, выполняемые внутри управляющего гостя."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path
from urllib.parse import urlparse

from .common import (
    InfraManagerError,
    console,
    require_runtime_activation_idle,
    run,
)
from .guest_deploy import project_branch_for_checkout, project_revision
from .guest_operations import list_local_guests, run_local_guest_status
from .operation_lock import project_checkout_lock
from .pve import PveClient
from .pve_host import (
    preflight_recovery_contour,
    repair_openbao_on_host,
    show_openbao_operator_credentials,
)
from .settings import PATHS, SETTINGS

ACTIVATE_RUNTIME = Path("/usr/local/sbin/infra-manager-activate-runtime")
COMPOSE_FILE = PATHS.compose_dir / "docker-compose.yml"
VERSIONS_FILE = PATHS.compose_dir / ".versions.env"
RUNTIME_CONTAINER = "infra-runtime"
RUNTIME_GUEST_OPERATION = (
    PATHS.repo_root / "scripts" / "infra-manager" / "jobs" / "guest-operation.py"
)


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


def operator_update() -> int:
    """Обновить постоянную рабочую копию проекта из выбранной Git-ветки."""

    require_runtime_activation_idle()
    repo_root = PATHS.repo_root
    git_dir = repo_root / ".git"
    known_hosts = PATHS.semaphore_dir / "known_hosts"
    for path, label in (
        (git_dir, "Git-репозиторий проекта"),
        (PATHS.github_key, "GitHub Deploy Key"),
        (known_hosts, "GitHub known_hosts"),
    ):
        if not path.exists():
            raise InfraManagerError(f"Не найден {label}: {path}")

    branch = SETTINGS.project_branch().strip()
    if not branch or any(character.isspace() for character in branch):
        raise InfraManagerError("Некорректная Git-ветка проекта")

    ssh_command = shlex.join(
        [
            "ssh",
            "-i",
            str(PATHS.github_key),
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
        ]
    )
    environment = os.environ.copy()
    environment["GIT_SSH_COMMAND"] = ssh_command

    with project_checkout_lock(exclusive=True):
        console.info(f"Получение Git-ветки {branch}")
        run(
            [
                "git",
                "-C",
                str(repo_root),
                "fetch",
                "--depth",
                "1",
                "origin",
                branch,
            ],
            env=environment,
        )
        run(
            ["git", "-C", str(repo_root), "reset", "--hard", "FETCH_HEAD"]
        )
        run(["git", "-C", str(repo_root), "clean", "-ffdx"])

    console.result(
        f"Рабочая копия проекта обновлена: {branch} "
        f"({project_revision(repo_root)})"
    )
    return 0


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
    """Выполнить штатную операцию напрямую внутри infra-runtime."""

    require_runtime_activation_idle()
    if not RUNTIME_GUEST_OPERATION.is_file():
        raise InfraManagerError(
            f"Не найдена точка входа операций: {RUNTIME_GUEST_OPERATION}"
        )

    result = run(
        [
            "docker",
            "exec",
            "--user",
            "1001:0",
            RUNTIME_CONTAINER,
            "python3",
            str(RUNTIME_GUEST_OPERATION),
            operation,
            str(vmid),
        ],
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    if operation == "test":
        return 0
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

    subparsers.add_parser(
        "update",
        help="обновить постоянную рабочую копию проекта из Git",
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
        if args.command == "update":
            return operator_update()
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
