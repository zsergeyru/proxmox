#!/usr/bin/env python3
"""Базовые проверки общего фундамента Python-пакета infra-manager."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.cli import main
import infra_manager.common as common
from infra_manager.common import (
    CommandError,
    CommandRunner,
    Console,
    InfraManagerError,
    _redact_argv,
    log_level,
    run,
)
from infra_manager.pve import (
    PveClient,
    permission_present,
    select_management_ipv4,
)
from infra_manager.settings import Paths


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


def check_log_levels() -> None:
    old_level = os.environ.get("INFRA_LOG_LEVEL")
    try:
        os.environ.pop("INFRA_LOG_LEVEL", None)
        if log_level() != "normal":
            fail("Уровень вывода по умолчанию должен быть normal")

        normal_out = io.StringIO()
        normal_console = Console(out=normal_out, err=io.StringIO())
        normal_console.info("этап")
        normal_console.detail("подробность")
        normal_console.ok("готово")
        normal_console.result("итог")
        normal_text = normal_out.getvalue()
        if "этап" not in normal_text or "готово" not in normal_text:
            fail("normal должен показывать основные сообщения")
        if "подробность" in normal_text:
            fail("normal не должен показывать диагностические подробности")
        if "итог" not in normal_text:
            fail("normal должен показывать итог")

        os.environ["INFRA_LOG_LEVEL"] = "verbose"
        verbose_out = io.StringIO()
        verbose_console = Console(out=verbose_out, err=io.StringIO())
        verbose_console.detail("подробность")
        if "подробность" not in verbose_out.getvalue():
            fail("verbose должен показывать диагностические подробности")

        os.environ["INFRA_LOG_LEVEL"] = "quiet"
        quiet_out = io.StringIO()
        quiet_console = Console(out=quiet_out, err=io.StringIO())
        quiet_console.info("этап")
        quiet_console.ok("готово")
        quiet_console.result("итог")
        quiet_text = quiet_out.getvalue()
        if "этап" in quiet_text or "готово" in quiet_text:
            fail("quiet не должен показывать промежуточные сообщения")
        if "итог" not in quiet_text:
            fail("quiet должен показывать итог")

        os.environ["INFRA_LOG_LEVEL"] = "invalid"
        try:
            log_level()
        except InfraManagerError:
            pass
        else:
            fail("Некорректный INFRA_LOG_LEVEL должен вызывать ошибку")

        os.environ["INFRA_LOG_LEVEL"] = "normal"
        runner = CommandRunner()
        hidden = runner.run(
            [sys.executable, "-c", "print('HIDDEN-SUCCESS')"]
        )
        if hidden.stdout.strip() != "HIDDEN-SUCCESS":
            fail("normal должен перехватывать подробный stdout успешной команды")

        try:
            runner.run(
                [
                    sys.executable,
                    "-c",
                    "print('FAIL-DETAIL'); raise SystemExit(9)",
                ]
            )
        except CommandError as exc:
            if "FAIL-DETAIL" not in str(exc):
                fail("normal должен сохранять диагностический вывод ошибки")
        else:
            fail("Ошибка внешней команды не была обнаружена")
    finally:
        if old_level is None:
            os.environ.pop("INFRA_LOG_LEVEL", None)
        else:
            os.environ["INFRA_LOG_LEVEL"] = old_level


def check_runtime_activation_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "runtime-activation-pending"
        original = common.RUNTIME_ACTIVATION_MARKER
        common.RUNTIME_ACTIVATION_MARKER = marker
        try:
            common.require_runtime_activation_idle()
            common.reserve_runtime_activation()
            if marker.read_text(encoding="utf-8").strip() != str(os.getpid()):
                fail("Маркер активации должен содержать PID текущего задания")
            if marker.stat().st_mode & 0o777 != 0o600:
                fail("Маркер активации должен иметь права 0600")
            try:
                common.require_runtime_activation_idle()
            except InfraManagerError:
                pass
            else:
                fail("Новое задание не заблокировано во время активации runtime")
            try:
                common.reserve_runtime_activation()
            except InfraManagerError:
                pass
            else:
                fail("Повторное резервирование активации не вызвало ошибку")
            common.cancel_runtime_activation()
            if marker.exists():
                fail("Отмена активации не удалила маркер")
        finally:
            common.RUNTIME_ACTIVATION_MARKER = original
            marker.unlink(missing_ok=True)


def check_path_overrides() -> None:
    old_config = os.environ.get("INFRA_MANAGER_CONFIG_DIR")
    old_data = os.environ.get("INFRA_MANAGER_DATA_DIR")
    try:
        os.environ["INFRA_MANAGER_CONFIG_DIR"] = "/etc/bootstrap-runner"
        os.environ["INFRA_MANAGER_DATA_DIR"] = "/var/lib/bootstrap-runner"
        paths = Paths()
        if paths.config_dir != Path("/etc/bootstrap-runner"):
            fail(f"Переопределение config_dir не применилось: {paths.config_dir}")
        if paths.data_dir != Path("/var/lib/bootstrap-runner"):
            fail(f"Переопределение data_dir не применилось: {paths.data_dir}")
        if paths.opentofu_state_dir != Path("/var/lib/bootstrap-runner/opentofu/state"):
            fail("Начальный контур получил неверный каталог состояния OpenTofu")
    finally:
        if old_config is None:
            os.environ.pop("INFRA_MANAGER_CONFIG_DIR", None)
        else:
            os.environ["INFRA_MANAGER_CONFIG_DIR"] = old_config
        if old_data is None:
            os.environ.pop("INFRA_MANAGER_DATA_DIR", None)
        else:
            os.environ["INFRA_MANAGER_DATA_DIR"] = old_data


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
    check_log_levels()
    check_runtime_activation_guard()
    check_path_overrides()
    check_command_runner_contract()
    check_pve_helpers()
    print("Базовые проверки Python-пакета infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
