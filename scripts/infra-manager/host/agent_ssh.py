#!/usr/bin/env python3
"""Принудительная SSH-команда пользователя infra-agent на PVE.

Все параметры поступают из SSH_ORIGINAL_COMMAND. Произвольная оболочка
и выполнение внешних программ запрещены.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

EXECUTOR = "/usr/local/sbin/infra-manager-agent"
COMMANDS = frozenset({"guests", "status", "test", "deploy", "sync", "repair"})


def parse_request(original: str) -> list[str]:
    if not original or len(original) > 128:
        raise ValueError("Пустая или слишком длинная команда")
    try:
        parts = shlex.split(original, posix=True)
    except ValueError as exc:
        raise ValueError("Неверные аргументы SSH") from exc
    if not parts or parts[0] != "infra-manager":
        raise ValueError("Разрешена только команда infra-manager")
    arguments = parts[1:]
    if not arguments or arguments[0] not in COMMANDS:
        raise ValueError("Команда не разрешена")
    if len(arguments) > 2 or any(not item.isascii() for item in arguments):
        raise ValueError("Неожиданные аргументы")
    if len(arguments) == 2:
        vmid = arguments[1]
        if not vmid.isdecimal() or not 3 <= len(vmid) <= 6:
            raise ValueError("VMID должен быть числом")
    elif arguments[0] not in {"guests", "status"}:
        raise ValueError("Команде требуется VMID")
    return arguments


def main() -> int:
    try:
        arguments = parse_request(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    try:
        return subprocess.run(
            ["/usr/bin/sudo", "-n", EXECUTOR, "--execute", *arguments],
            check=False,
        ).returncode
    except OSError:
        print("ОШИБКА: исполнитель недоступен", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
