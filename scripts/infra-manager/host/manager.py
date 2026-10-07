#!/usr/bin/env python3
"""Единая операторская команда infra-manager на PVE."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HOST_CONFIG = Path("/etc/infra-manager/openbao-host.json")
RECOVERY_COMMAND = Path("/usr/local/sbin/infra-manager-recovery")
OPENBAO_COMMAND = Path("/usr/local/sbin/infra-manager-openbao-unseal")
BOOTSTRAP_URL = (
    "https://raw.githubusercontent.com/"
    "zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh"
)
COMPOSE_DIR = Path("/opt/infra-manager/compose")
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"
VERSIONS_FILE = COMPOSE_DIR / ".versions.env"
PROJECT_DIR = Path("/var/lib/infra-manager/bootstrap-repo")
ACTIVATE_RUNTIME = Path("/usr/local/sbin/infra-manager-activate-runtime")
LOCK_PATH = Path("/run/lock/infra-manager-operator.lock")


class OperatorError(RuntimeError):
    pass


def run(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=capture,
            check=False,
        )
    except OSError as exc:
        raise OperatorError(
            f"Не удалось запустить команду {' '.join(argv)}: {exc}"
        ) from exc
    if check and result.returncode:
        detail = ""
        if capture:
            detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise OperatorError(
            f"Команда завершилась с кодом {result.returncode}: "
            f"{' '.join(argv)}{suffix}"
        )
    return result


def require_root() -> None:
    if os.geteuid() != 0:
        raise OperatorError("infra-manager должен выполняться от root на PVE")


def require_executable(path: Path) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise OperatorError(f"Не найдена служебная команда: {path}")


def load_identity() -> tuple[int, str]:
    try:
        payload = json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorError(
            f"Не удалось прочитать описание infra-manager: {HOST_CONFIG}"
        ) from exc
    vmid = payload.get("vmid") if isinstance(payload, dict) else None
    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(vmid, int) or vmid <= 0:
        raise OperatorError("Описание infra-manager содержит некорректный VMID")
    if not isinstance(name, str) or not name:
        raise OperatorError("Описание infra-manager содержит некорректное имя")
    return vmid, name


def guest_config(vmid: int) -> str | None:
    result = run(
        ["pct", "config", str(vmid)],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def verify_guest_owned(vmid: int, name: str) -> None:
    config = guest_config(vmid)
    if config is None:
        raise OperatorError(
            f"Гость infra-manager {vmid} отсутствует; "
            "для восстановления используйте 'infra-manager recover'"
        )
    lines = config.splitlines()
    hostname_ok = f"hostname: {name}" in lines
    unprivileged_ok = "unprivileged: 1" in lines
    description = next(
        (line for line in lines if line.startswith("description: ")),
        "",
    )
    owner_ok = "owner=proxmox-project" in description
    role_ok = "role=infra-manager" in description
    if not (hostname_ok and unprivileged_ok and owner_ok and role_ok):
        raise OperatorError(
            f"VMID {vmid} не имеет строгой метки владения infra-manager; "
            "автоматические действия запрещены"
        )


def guest_running(vmid: int) -> bool:
    result = run(
        ["pct", "status", str(vmid)],
        check=False,
        capture=True,
    )
    return result.returncode == 0 and result.stdout.strip().endswith("running")


def exec_guest(
    vmid: int,
    *argv: str,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["pct", "exec", str(vmid), "--", *argv],
        check=check,
        capture=capture,
    )


def recovery_check(mode: str) -> None:
    require_executable(RECOVERY_COMMAND)
    run([str(RECOVERY_COMMAND), mode])


def manager_status() -> int:
    vmid, name = load_identity()
    failures: list[str] = []
    require_executable(RECOVERY_COMMAND)

    print("==> PVE-only аварийный контур")
    result = run(
        [str(RECOVERY_COMMAND), "--check"],
        check=False,
    )
    if result.returncode:
        failures.append("аварийный контур")

    print(f"\n==> {vmid} {name}")
    try:
        verify_guest_owned(vmid, name)
    except OperatorError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        failures.append("объект infra-manager")
    else:
        if not guest_running(vmid):
            print(f"ОШИБКА: LXC {vmid} остановлен", file=sys.stderr)
            failures.append("LXC остановлен")
        else:
            result = exec_guest(
                vmid,
                "infra-manager-status",
                "--full",
                check=False,
            )
            if result.returncode:
                failures.append("полная проверка infra-manager")

    if failures:
        print(
            "\nОШИБКА: infra-manager не готов: " + ", ".join(failures),
            file=sys.stderr,
        )
        return 1

    print("\n==> OpenBao UI")
    _show_openbao_credentials()
    print("\n[ОК] infra-manager полностью готов")
    return 0


def guest_status(vmid: int) -> int:
    """Показать статус выбранного гостя с секретами в root-сессии PVE."""

    manager_vmid, manager_name = load_identity()
    verify_guest_owned(manager_vmid, manager_name)
    if not guest_running(manager_vmid):
        raise OperatorError(f"LXC {manager_vmid} остановлен")

    result = exec_guest(
        manager_vmid,
        "env",
        "PYTHONPATH=/usr/local/lib/infra-manager",
        "python3",
        "-m",
        "infra_manager",
        "guest-status",
        "--guest-vmid",
        str(vmid),
        "--show-secrets",
        check=False,
    )
    if result.returncode:
        raise OperatorError(
            f"Не удалось получить статус гостя {vmid} через infra-manager"
        )
    return 0


def guest_list() -> int:
    """Показать каталог гостей через установленный управляющий код."""

    manager_vmid, manager_name = load_identity()
    verify_guest_owned(manager_vmid, manager_name)
    if not guest_running(manager_vmid):
        raise OperatorError(f"LXC {manager_vmid} остановлен")

    result = exec_guest(
        manager_vmid,
        "env",
        "PYTHONPATH=/usr/local/lib/infra-manager",
        "python3",
        "-m",
        "infra_manager",
        "guest-list",
        check=False,
    )
    if result.returncode:
        raise OperatorError("Не удалось получить список гостей через infra-manager")
    return 0


def guest_task(operation: str, vmid: int) -> int:
    """Передать изменение гостя штатному заданию Semaphore."""

    manager_vmid, manager_name = load_identity()
    verify_guest_owned(manager_vmid, manager_name)
    if not guest_running(manager_vmid):
        raise OperatorError(f"LXC {manager_vmid} остановлен")

    result = exec_guest(
        manager_vmid,
        "env",
        "PYTHONPATH=/usr/local/lib/infra-manager",
        "python3",
        "-m",
        "infra_manager",
        "guest-task",
        "--operation",
        operation,
        "--guest-vmid",
        str(vmid),
        check=False,
    )
    if result.returncode:
        raise OperatorError(
            f"Semaphore не выполнил {operation} для гостя {vmid}"
        )

    if operation == "test":
        return 0

    for attempt in range(30):
        try:
            return guest_status(vmid)
        except OperatorError:
            if attempt == 29:
                raise
            __import__("time").sleep(2)
    return 0


def _wait_running(vmid: int) -> None:
    if guest_running(vmid):
        return
    run(["pct", "start", str(vmid)])
    for _ in range(30):
        if guest_running(vmid):
            return
        __import__("time").sleep(1)
    raise OperatorError(f"LXC {vmid} не запустился за 30 секунд")


def _project_branch(vmid: int) -> str:
    result = exec_guest(
        vmid,
        "git",
        "-C",
        str(PROJECT_DIR),
        "branch",
        "--show-current",
        check=False,
        capture=True,
    )
    branch = result.stdout.strip() if result.returncode == 0 else ""
    return branch or "main"


def repair() -> int:
    vmid, name = load_identity()

    print("==> Проверка сохранности постоянного состояния")
    recovery_check("--preflight")

    verify_guest_owned(vmid, name)
    _wait_running(vmid)

    print("==> Подготовка Docker и OpenBao")
    exec_guest(vmid, "systemctl", "start", "docker")
    for required in (COMPOSE_FILE, VERSIONS_FILE):
        result = exec_guest(
            vmid,
            "test",
            "-s",
            str(required),
            check=False,
        )
        if result.returncode:
            raise OperatorError(
                f"В {vmid} отсутствует {required}; "
                "обычное исправление остановлено, используйте recovery-контур"
            )

    exec_guest(
        vmid,
        "docker",
        "compose",
        "--env-file",
        str(VERSIONS_FILE),
        "-f",
        str(COMPOSE_FILE),
        "up",
        "-d",
        "openbao",
    )

    print("==> Проверка и безопасное восстановление OpenBao")
    require_executable(OPENBAO_COMMAND)
    run(
        [
            str(OPENBAO_COMMAND),
            "--repair",
            "--log-level",
            "normal",
        ]
    )

    print("==> Активация управляющей среды")
    branch = _project_branch(vmid)
    pve_node = socket.gethostname().split(".")[0]
    exec_guest(
        vmid,
        "env",
        f"INFRA_PROJECT_BRANCH={branch}",
        f"INFRA_PVE_NODE={pve_node}",
        str(ACTIVATE_RUNTIME),
    )

    print("==> Итоговая проверка")
    return manager_status()


def _show_openbao_credentials() -> int:
    """Показать данные входа OpenBao как часть доверенного PVE status."""

    require_executable(OPENBAO_COMMAND)
    run(
        [
            str(OPENBAO_COMMAND),
            "--show-operator-credentials",
            "--log-level",
            "quiet",
        ]
    )
    return 0


def _download_bootstrap(target: Path) -> None:
    try:
        with urllib.request.urlopen(BOOTSTRAP_URL, timeout=30) as response:
            payload = response.read(1024 * 1024 + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise OperatorError(
            f"Не удалось получить bootstrap recovery: {exc}"
        ) from exc
    if not payload or len(payload) > 1024 * 1024:
        raise OperatorError("Получен некорректный bootstrap recovery")
    if not payload.startswith(b"#!"):
        raise OperatorError("Bootstrap recovery не похож на исполняемый сценарий")
    target.write_bytes(payload)
    target.chmod(0o700)


def recover() -> int:
    print("==> Проверка возможности восстановления")
    recovery_check("--preflight")

    print("==> Восстановление bootstrap Git-доступа")
    recovery_check("--restore-git-access")

    print("==> Запуск штатного recovery-контура")
    with tempfile.TemporaryDirectory(prefix="infra-manager-recover.") as tmp:
        bootstrap = Path(tmp) / "bootstrap-pve.sh"
        _download_bootstrap(bootstrap)
        run(["bash", str(bootstrap), "--recover"])

    print("==> Итоговая проверка")
    return manager_status()


def acquire_operator_lock():
    """Не допустить одновременный repair/recover."""

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    stream = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise OperatorError(
            "Другая операция infra-manager repair/recover уже выполняется"
        ) from exc
    return stream


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверка и восстановление infra-manager с PVE"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser(
        "status",
        help="Проверить управляющий контур или выбранного гостя",
    )
    status_parser.add_argument(
        "vmid",
        nargs="?",
        type=int,
        help="VMID гостя; без VMID проверяется управляющий контур",
    )
    subparsers.add_parser(
        "guests",
        help="Показать поддерживаемых гостей и их состояние",
    )

    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Развернуть или обновить гостя через Semaphore",
    )
    deploy_parser.add_argument("vmid", type=int, help="VMID гостя")

    sync_parser = subparsers.add_parser(
        "sync",
        help="Синхронизировать гостя через Semaphore",
    )
    sync_parser.add_argument("vmid", type=int, help="VMID гостя")

    repair_parser = subparsers.add_parser(
        "repair",
        help="Исправить управляющий контур или выбранного гостя",
    )
    repair_parser.add_argument(
        "vmid",
        nargs="?",
        type=int,
        help="VMID гостя; без VMID исправляется управляющий контур",
    )

    test_parser = subparsers.add_parser(
        "test",
        help="Проверить гостя через Semaphore без изменения состояния",
    )
    test_parser.add_argument("vmid", type=int, help="VMID гостя")

    subparsers.add_parser(
        "recover",
        help="Запустить защищённое аварийное восстановление через bootstrap",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_root()
    if args.command == "status":
        if args.vmid is None:
            return manager_status()
        return guest_status(args.vmid)
    if args.command == "guests":
        return guest_list()

    with acquire_operator_lock():
        if args.command == "deploy":
            return guest_task("deploy", args.vmid)
        if args.command == "sync":
            return guest_task("sync", args.vmid)
        if args.command == "repair":
            if args.vmid is None:
                return repair()
            return guest_task("repair", args.vmid)
        if args.command == "test":
            return guest_task("test", args.vmid)
        if args.command == "recover":
            return recover()
    raise OperatorError(f"Неизвестная команда: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OperatorError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1)
