#!/usr/bin/env python3
"""Базовые проверки общего фундамента Python-пакета infra-manager."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.cli import main  # noqa: E402
from infra_manager.common import (  # noqa: E402
    CommandError,
    CommandRunner,
    InfraManagerError,
    _redact_argv,
    run,
)
from infra_manager.pve import (  # noqa: E402
    PveClient,
    permission_present,
    select_management_ipv4,
)


def fail(message: str) -> None:
    raise SystemExit(message)


def check_cli_and_commands() -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main([])
    if rc != 0 or "infra-manager" not in stdout.getvalue():
        fail("CLI infra-manager не вывел справку при запуске без аргументов")

    result = run([sys.executable, "-c", "print('OK')"], capture_output=True)
    if result.stdout.strip() != "OK":
        fail("common.run не вернул stdout внешней команды")

    try:
        run(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            capture_output=True,
        )
    except CommandError as exc:
        if exc.returncode != 7:
            fail(f"CommandError содержит неверный код возврата: {exc.returncode}")
    else:
        fail("common.run не сообщил об ошибке внешней команды")

    runner = CommandRunner()
    captured = runner.run(
        [sys.executable, "-c", "print('CAPTURED')"],
        capture=True,
    )
    if captured.stdout.strip() != "CAPTURED":
        fail("CommandRunner в режиме capture не вернул stdout")

    with tempfile.TemporaryDirectory() as tmp:
        command_log = Path(tmp) / "command.log"
        logged_runner = CommandRunner(default_log_file=command_log)
        logged_runner.run(
            [sys.executable, "-c", "print('LOGGED')", "--api-key", "secret-value"],
            sensitive_args=(4,),
        )
        log_text = command_log.read_text(encoding="utf-8")
        if "LOGGED" not in log_text:
            fail("CommandRunner не записал вывод команды в журнал")
        if "secret-value" in log_text:
            fail("CommandRunner записал чувствительный аргумент в журнал")

    redacted = _redact_argv(
        [
            "tool",
            "--token",
            "abc",
            "--password=def",
            "-var=proxmox_token=ghi",
            "-var=other=value",
        ]
    )
    if redacted != (
        "tool",
        "--token",
        "[СКРЫТО]",
        "--password=[СКРЫТО]",
        "-var=proxmox_token=[СКРЫТО]",
        "-var=other=value",
    ):
        fail(f"Чувствительные аргументы скрыты некорректно: {redacted!r}")

    nested = _redact_argv(["tool", "-var=proxmox_token=abc=def", "next"])
    if nested != ("tool", "-var=proxmox_token=[СКРЫТО]", "next"):
        fail(f"Вложенный знак '=' обработан некорректно: {nested!r}")

    explicit = _redact_argv(
        ["tool", "--api-key", "secret-value", "next"],
        sensitive_indices=(2,),
    )
    if explicit != ("tool", "--api-key", "[СКРЫТО]", "next"):
        fail(f"sensitive_indices обработан некорректно: {explicit!r}")

    cli_help_cases = (
        ["--help"],
        ["setup", "--help"],
        ["semaphore-project", "--help"],
        ["status", "--help"],
        ["pve-access-check", "--help"],
    )
    for args in cli_help_cases:
        direct = subprocess.run(
            [sys.executable, "-m", "infra_manager", *args],
            cwd=MODULE_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if direct.returncode != 0:
            fail(
                f"python -m infra_manager {' '.join(args)} "
                f"завершился с кодом {direct.returncode}"
            )
        if "usage: infra-manager" not in direct.stdout:
            fail(f"python -m infra_manager {' '.join(args)} не вывел строку использования")


def check_command_runner_contract() -> None:
    package_dir = MODULE_ROOT / "infra_manager"
    for module_path in package_dir.glob("*.py"):
        if module_path.name == "common.py":
            continue
        source = module_path.read_text(encoding="utf-8")
        if "subprocess.run(" in source:
            fail(
                "Внешние команды должны выполняться только через CommandRunner: "
                f"{module_path.name}"
            )


def check_pve_helpers() -> None:
    permissions = {"/vms": {"VM.Audit": 1, "VM.Allocate": True}}
    if not permission_present(permissions, "VM.Audit"):
        fail("permission_present не обнаружил числовую привилегию")
    if not permission_present(permissions, "VM.Allocate"):
        fail("permission_present не обнаружил логическую привилегию")
    if permission_present(permissions, "Permissions.Modify"):
        fail("permission_present обнаружил отсутствующую привилегию")

    vm_interfaces = {
        "result": [
            {
                "name": "lo",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "127.0.0.1"},
                ],
            },
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.77"},
                    {"ip-address-type": "ipv6", "ip-address": "fe80::1"},
                ],
            },
            {
                "name": "docker0",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "172.17.0.1"},
                ],
            },
        ],
    }
    selected = select_management_ipv4(vm_interfaces, "192.168.0.0/16")
    if str(selected) != "192.168.1.77":
        fail(f"Для VM выбран неверный IPv4: {selected}")

    lxc_interfaces = [
        {
            "name": "eth0",
            "ip-addresses": [
                {"ip-address-type": "inet", "ip-address": "192.168.2.40"},
            ],
        },
    ]
    selected = select_management_ipv4(lxc_interfaces, "192.168.0.0/16")
    if str(selected) != "192.168.2.40":
        fail(f"Для LXC выбран неверный IPv4: {selected}")

    if select_management_ipv4(vm_interfaces, "10.0.0.0/8") is not None:
        fail("Поиск DHCP-адреса обнаружил IPv4 вне административной подсети")

    ambiguous = {
        "result": [
            {
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.10"},
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.11"},
                ],
            },
        ],
    }
    try:
        select_management_ipv4(ambiguous, "192.168.0.0/16")
    except InfraManagerError:
        pass
    else:
        fail("Неоднозначный административный IPv4 не вызвал ошибку")

    calls: list[str] = []
    client = object.__new__(PveClient)
    client.data = lambda endpoint, **kwargs: calls.append(endpoint) or []
    client.guest_interfaces(node="pve", vmid=410, kind="vm")
    if calls[-1] != "/nodes/pve/qemu/410/agent/network-get-interfaces":
        fail(f"Выбран неверный конечный адрес PVE API для VM: {calls[-1]}")
    client.guest_interfaces(node="pve", vmid=311, kind="lxc")
    if calls[-1] != "/nodes/pve/lxc/311/interfaces":
        fail(f"Выбран неверный конечный адрес PVE API для LXC: {calls[-1]}")


def main_test() -> None:
    check_cli_and_commands()
    check_command_runner_contract()
    check_pve_helpers()
    print("Базовые проверки Python-пакета infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
