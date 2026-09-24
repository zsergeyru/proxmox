#!/usr/bin/env python3
"""Минимальные unit/contract checks Python-части infra-manager."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.cli import main  # noqa: E402
from infra_manager.common import (  # noqa: E402
    CommandError,
    InfraManagerError,
    _redact_argv,
    run,
)
from infra_manager.opentofu import _find_guest_directory  # noqa: E402
from infra_manager.pve import (  # noqa: E402
    PveClient,
    permission_present,
    select_management_ipv4,
)
from infra_manager.template import (  # noqa: E402
    _packer_inputs,
    _template_failures,
    _validate_cloud_status,
)
from infra_manager.semaphore import PROJECT_REPO, SemaphoreClient  # noqa: E402
from infra_manager.status import (  # noqa: E402
    _check_git_branch_contract,
    _project_branch,
)
from infra_manager.setup import BASE_PACKAGES, Setup  # noqa: E402
from infra_manager.settings import PATHS, SETTINGS  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main([])
    if rc != 0 or "infra-manager" not in stdout.getvalue():
        fail("infra-manager CLI без аргументов не вывел справку")

    result = run(
        [sys.executable, "-c", "print('OK')"],
        capture_output=True,
    )
    if result.stdout.strip() != "OK":
        fail("common.run не вернул stdout внешней команды")

    try:
        run(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            capture_output=True,
        )
    except CommandError as exc:
        if exc.returncode != 7:
            fail(
                f"CommandError содержит неверный returncode: "
                f"{exc.returncode}"
            )
    else:
        fail("common.run не сообщил об ошибке внешней команды")

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
        fail(f"Некорректное редактирование чувствительных аргументов: {redacted!r}")

    nested = _redact_argv(
        ["tool", "-var=proxmox_token=abc=def", "next"]
    )
    if nested != (
        "tool",
        "-var=proxmox_token=[СКРЫТО]",
        "next",
    ):
        fail(f"Вложенный '=' обработан неверно: {nested!r}")

    explicit = _redact_argv(
        ["tool", "--api-key", "secret-value", "next"],
        sensitive_indices=(2,),
    )
    if explicit != ("tool", "--api-key", "[СКРЫТО]", "next"):
        fail(f"sensitive_indices обработан неверно: {explicit!r}")

    packer_client = SimpleNamespace(
        url="https://pve.example:8006",
        token_id="root@pam!infra-manager",
        token_secret="pve-secret",
    )
    packer_env, packer_args = _packer_inputs(
        {"BASE": "1"},
        client=packer_client,
        node="pve",
        iso_url="https://example.invalid/debian.iso",
        iso_checksum="sha512:deadbeef",
        build_password="build-secret",
    )
    if packer_env.get("PKR_VAR_proxmox_token") != "pve-secret":
        fail("Packer API token не передан через PKR_VAR_proxmox_token")
    if packer_env.get("PKR_VAR_build_password") != "build-secret":
        fail("Packer build password не передан через PKR_VAR_build_password")
    joined_packer_args = " ".join(packer_args)
    if "pve-secret" in joined_packer_args or "build-secret" in joined_packer_args:
        fail("Секрет Packer попал в аргументы командной строки")
    if any(
        arg.startswith("-var=proxmox_token=")
        or arg.startswith("-var=build_password=")
        for arg in packer_args
    ):
        fail("Секретные переменные Packer всё ещё передаются через -var")

    previous_branch = os.environ.get("INFRA_PROJECT_BRANCH")
    try:
        os.environ["INFRA_PROJECT_BRANCH"] = "feature/test-branch"
        if _project_branch() != "feature/test-branch":
            fail("status не учитывает INFRA_PROJECT_BRANCH")
    finally:
        if previous_branch is None:
            os.environ.pop("INFRA_PROJECT_BRANCH", None)
        else:
            os.environ["INFRA_PROJECT_BRANCH"] = previous_branch

    branch = "feature/test-branch"
    repository = {
        "git_url": PROJECT_REPO,
        "ssh_key_id": 7,
        "git_branch": branch,
    }
    templates = [
        {"name": "OpenTofu Plan", "git_branch": branch},
        {"name": "Build Template 9000", "git_branch": branch},
        {"name": "Deploy Guest 410", "git_branch": branch},
    ]
    _check_git_branch_contract(
        repository,
        ["main", branch],
        templates,
        github_key_id=7,
        project_branch=branch,
    )

    wrong_repository = dict(repository)
    wrong_repository["git_branch"] = "main"
    try:
        _check_git_branch_contract(
            wrong_repository,
            ["main", branch],
            templates,
            github_key_id=7,
            project_branch=branch,
        )
    except InfraManagerError:
        pass
    else:
        fail("status принял repository.git_branch другой ветки")

    wrong_templates = [dict(item) for item in templates]
    wrong_templates[1]["git_branch"] = "main"
    try:
        _check_git_branch_contract(
            repository,
            ["main", branch],
            wrong_templates,
            github_key_id=7,
            project_branch=branch,
        )
    except InfraManagerError:
        pass
    else:
        fail("status принял template.git_branch другой ветки")

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
                f"вернул {direct.returncode}"
            )
        if "usage: infra-manager" not in direct.stdout:
            fail(
                f"python -m infra_manager {' '.join(args)} "
                "не вывел usage"
            )

    if "python3" not in BASE_PACKAGES:
        fail("python3 отсутствует в контракте setup")
    if "python3-yaml" not in BASE_PACKAGES:
        fail("python3-yaml отсутствует в контракте setup")

    if PATHS.pve_api_env != Path("/etc/infra-manager/secrets/pve-api.env"):
        fail("Единый путь PVE credential имеет неожиданное значение")
    if PATHS.ca_bundle != Path("/etc/infra-manager/ca/ca-bundle.crt"):
        fail("Единый путь CA bundle имеет неожиданное значение")
    if PATHS.opentofu_input != Path("/var/lib/infra-manager/opentofu/guests.json"):
        fail("Единый путь OpenTofu input имеет неожиданное значение")
    if SETTINGS.default_project_branch != "main":
        fail("Ветка проекта по умолчанию должна быть main")
    if SETTINGS.packer_version != "1.15.4":
        fail("Версия Packer в единых настройках изменилась неожиданно")

    compose = Setup.compose("ps")
    if compose[:2] != ["docker", "compose"] or compose[-1] != "ps":
        fail(f"Некорректная команда Docker Compose: {compose!r}")

    one = SemaphoreClient.unique_by_name(
        [{"id": 1, "name": "proxmox"}],
        "proxmox",
        "Git repository",
    )
    if one is None or one.get("id") != 1:
        fail("Semaphore unique_by_name не вернул единственный объект")

    none = SemaphoreClient.unique_by_name(
        [{"id": 1, "name": "other"}],
        "proxmox",
        "Git repository",
    )
    if none is not None:
        fail("Semaphore unique_by_name не вернул None для отсутствующего объекта")

    try:
        SemaphoreClient.unique_by_name(
            [
                {"id": 1, "name": "proxmox"},
                {"id": 2, "name": "proxmox"},
            ],
            "proxmox",
            "Git repository",
        )
    except InfraManagerError:
        pass
    else:
        fail("Semaphore unique_by_name разрешил дубликаты")

    permissions = {
        "/vms": {
            "VM.Audit": 1,
            "VM.Allocate": True,
        }
    }
    if not permission_present(permissions, "VM.Audit"):
        fail("PVE permission_present не увидел числовое privilege")
    if not permission_present(permissions, "VM.Allocate"):
        fail("PVE permission_present не увидел boolean privilege")
    if permission_present(permissions, "Permissions.Modify"):
        fail("PVE permission_present нашёл отсутствующее privilege")

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
        fail(f"Неверно выбран IPv4 VM: {selected}")

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
        fail(f"Неверно выбран IPv4 LXC: {selected}")

    if select_management_ipv4(vm_interfaces, "10.0.0.0/8") is not None:
        fail("Поиск DHCP-адреса нашёл IPv4 вне административной подсети")

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
        fail(f"Неверный PVE endpoint VM: {calls[-1]}")
    client.guest_interfaces(node="pve", vmid=311, kind="lxc")
    if calls[-1] != "/nodes/pve/lxc/311/interfaces":
        fail(f"Неверный PVE endpoint LXC: {calls[-1]}")

    guest_dir = _find_guest_directory(ROOT, 410)
    if guest_dir.name != "410-ai-control":
        fail(f"Неверно найден каталог VM 410: {guest_dir}")

    valid_template = {
        "template": 1,
        "name": "tpl-debian13",
        "description": "template-version=8",
        "protection": 1,
        "scsihw": "virtio-scsi-single",
        "agent": "1",
        "ide0": "local-lvm:cloudinit",
        "ciuser": "root",
        "ipconfig0": "ip=dhcp",
        "ciupgrade": 0,
    }
    if _template_failures(valid_template):
        fail("Корректный шаблон 9000 не прошёл Python-проверку")

    invalid_template = dict(valid_template)
    invalid_template["ide0"] = ""
    if "ide0=<cloudinit отсутствует>" not in _template_failures(
        invalid_template
    ):
        fail("Python-проверка шаблона не обнаружила отсутствие Cloud-Init")

    _validate_cloud_status(
        '{"status":"done","init":{},"init-local":{},'
        '"modules-config":{},"modules-final":{}}'
    )

    print("infra-manager Python checks passed.")


if __name__ == "__main__":
    main_test()
