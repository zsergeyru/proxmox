#!/usr/bin/env python3
"""Минимальная операторская оболочка infra-manager на PVE."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HOST_CONFIG = Path("/etc/infra-manager/openbao-host.json")
RECOVERY_COMMAND = Path("/usr/local/sbin/infra-manager-recovery")
GUEST_COMMAND = "/usr/local/sbin/infra-manager"


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
        raise OperatorError(f"Не удалось запустить {' '.join(argv)}: {exc}") from exc
    if check and result.returncode:
        raise OperatorError(
            f"Команда завершилась с кодом {result.returncode}: {' '.join(argv)}"
        )
    return result


def manager_vmid() -> int:
    try:
        payload = json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorError(f"Не удалось прочитать {HOST_CONFIG}") from exc

    vmid = payload.get("vmid") if isinstance(payload, dict) else None
    if not isinstance(vmid, int) or vmid <= 0:
        raise OperatorError("Некорректный VMID infra-manager")
    return vmid


def verify_manager(vmid: int) -> None:
    result = run(
        ["pct", "config", str(vmid)],
        check=False,
        capture=True,
    )
    required = (
        "unprivileged: 1",
        "owner=proxmox-project",
        "role=infra-manager",
    )
    if result.returncode or any(item not in result.stdout for item in required):
        raise OperatorError(
            f"VMID {vmid} не подтверждён как infra-manager; "
            "используйте infra-manager recover"
        )


def guest_running(vmid: int) -> bool:
    result = run(
        ["pct", "status", str(vmid)],
        check=False,
        capture=True,
    )
    return result.returncode == 0 and result.stdout.strip().endswith("running")


def start_manager(vmid: int) -> None:
    if guest_running(vmid):
        return
    run(["pct", "start", str(vmid)])
    for _ in range(30):
        if guest_running(vmid):
            return
        time.sleep(1)
    raise OperatorError(
        f"LXC {vmid} не запустился; используйте infra-manager recover"
    )


def proxy_to_manager(arguments: list[str]) -> int:
    vmid = manager_vmid()
    verify_manager(vmid)
    if arguments == ["repair"]:
        start_manager(vmid)
    elif not guest_running(vmid):
        raise OperatorError(
            f"LXC {vmid} остановлен; используйте infra-manager repair "
            "или infra-manager recover"
        )

    return run(
        [
            "pct",
            "exec",
            str(vmid),
            "--",
            "env",
            f"INFRA_PVE_NODE={socket.gethostname().split('.')[0]}",
            GUEST_COMMAND,
            "--trusted-pve",
            *arguments,
        ],
        check=False,
    ).returncode


def recover() -> int:
    if not RECOVERY_COMMAND.is_file() or not os.access(RECOVERY_COMMAND, os.X_OK):
        raise OperatorError(f"Не найдена аварийная команда: {RECOVERY_COMMAND}")
    return run([str(RECOVERY_COMMAND), "--recover"], check=False).returncode


def main() -> int:
    if os.geteuid() != 0:
        raise OperatorError("infra-manager должен выполняться от root на PVE")
    arguments = sys.argv[1:] or ["--help"]
    if arguments == ["recover"]:
        return recover()
    return proxy_to_manager(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OperatorError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1)
