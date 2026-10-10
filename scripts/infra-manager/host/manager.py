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
AGENT_COMMAND = "/usr/local/sbin/infra-manager-agent"


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
        raise OperatorError("Не удалось запустить операцию. Проверьте наличие необходимых программ и права root на PVE.") from exc
    if check and result.returncode:
        raise OperatorError(
            f"Операция не завершена (код {result.returncode}). Проверьте состояние управляющего гостя; при необходимости выполните infra-manager repair."
        )
    return result


def manager_identity() -> tuple[int, str]:
    try:
        payload = json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorError(f"Не удалось прочитать {HOST_CONFIG}") from exc

    if not isinstance(payload, dict):
        raise OperatorError("Некорректное описание infra-manager")
    vmid, name = payload.get("vmid"), payload.get("name")
    if not isinstance(vmid, int) or vmid <= 0 or not isinstance(name, str) or not name:
        raise OperatorError("Некорректное описание infra-manager")
    return vmid, name


def verify_manager(vmid: int, name: str) -> None:
    result = run(
        ["pct", "config", str(vmid)],
        check=False,
        capture=True,
    )
    required = (
        f"hostname: {name}",
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


def proxy_to_manager(arguments: list[str], *, trusted: bool = True) -> int:
    vmid, name = manager_identity()
    verify_manager(vmid, name)
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
            f"INFRA_MANAGER_COLOR={color_mode()}",
            *(
                ["INFRA_ENABLE_PVE_AGENT_ACCESS=1"]
                if trusted and os.environ.get("INFRA_ENABLE_PVE_AGENT_ACCESS") == "1"
                else []
            ),
            GUEST_COMMAND,
            *(["--trusted-pve"] if trusted else []),
            *arguments,
        ],
        check=False,
    ).returncode


def color_mode() -> str:
    mode = os.environ.get("INFRA_MANAGER_COLOR", "auto")
    enabled = mode == "1" or (mode == "auto" and sys.stdout.isatty())
    return "1" if enabled and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" else "0"


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
    if arguments and arguments[0] == "agent-approve":
        if len(arguments) != 3:
            raise OperatorError("Использование: infra-manager agent-approve OPERATION VMID")
        return run([AGENT_COMMAND, "--approve", *arguments[1:]], check=False).returncode
    if arguments and arguments[0] == "--agent":
        if arguments[1:] == ["recover"] or not arguments[1:]:
            raise OperatorError("Агенту не разрешено восстановление PVE")
        return proxy_to_manager(arguments[1:], trusted=False)
    return proxy_to_manager(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OperatorError as exc:
        label = "\033[31mОШИБКА:\033[0m" if color_mode() == "1" else "ОШИБКА:"
        print(f"{label} {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Операция остановлена пользователем. Перед повторным запуском проверьте infra-manager status.", file=sys.stderr)
        raise SystemExit(130)
