#!/usr/bin/env python3
"""Минимальная операторская оболочка infra-manager на PVE."""

from __future__ import annotations

import json
import os
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


def manager_vmid() -> int:
    try:
        payload = json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorError(
            f"Не удалось прочитать описание infra-manager: {HOST_CONFIG}"
        ) from exc

    vmid = payload.get("vmid") if isinstance(payload, dict) else None
    if not isinstance(vmid, int) or vmid <= 0:
        raise OperatorError("Описание infra-manager содержит некорректный VMID")
    return vmid


def guest_running(vmid: int) -> bool:
    result = run(
        ["pct", "status", str(vmid)],
        check=False,
        capture=True,
    )
    return (
        result.returncode == 0
        and result.stdout.strip().endswith("running")
    )


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

    repair_manager = arguments == ["repair"]
    if repair_manager:
        start_manager(vmid)
    elif not guest_running(vmid):
        raise OperatorError(
            f"LXC {vmid} остановлен; сначала выполните "
            "infra-manager repair или infra-manager recover"
        )

    result = run(
        [
            "pct",
            "exec",
            str(vmid),
            "--",
            GUEST_COMMAND,
            "--trusted-pve",
            *arguments,
        ],
        check=False,
    )
    return result.returncode


def recover() -> int:
    if not RECOVERY_COMMAND.is_file() or not os.access(
        RECOVERY_COMMAND,
        os.X_OK,
    ):
        raise OperatorError(
            f"Не найдена аварийная команда: {RECOVERY_COMMAND}"
        )
    return run(
        [str(RECOVERY_COMMAND), "--recover"],
        check=False,
    ).returncode


def main() -> int:
    require_root()
    arguments = sys.argv[1:]
    if not arguments:
        arguments = ["--help"]

    if arguments == ["recover"]:
        return recover()

    return proxy_to_manager(arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OperatorError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1)
