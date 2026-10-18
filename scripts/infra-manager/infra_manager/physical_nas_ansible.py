"""Применение Ansible к физическому NAS без VMID, OpenTofu и Semaphore."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .common import InfraManagerError, run, console
from .physical_host_ssh import PhysicalHostSshSession


def configure_nas(repo_root: Path, session: PhysicalHostSshSession) -> None:
    """Применить безопасную базовую настройку к уже установленной OMV 8."""

    target = session.host
    if not target.address:
        raise InfraManagerError("Не задан адрес физического NAS")
    playbook = repo_root / "automation" / "ansible" / "playbooks" / "configure-nas.yml"
    if not playbook.is_file():
        raise InfraManagerError(f"Отсутствует Ansible-сценарий NAS: {playbook}")
    key, ssh_args = session.ansible_parameters()
    environment = os.environ.copy()
    environment.update({
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOG_PATH": "",
    })
    args = [
        "ansible-playbook", str(playbook),
        "-i", f"{target.address},",
        "-u", target.user,
        "--private-key", key,
        "-e", json.dumps({
            "ansible_host": target.address,
            "ansible_ssh_common_args": ssh_args,
        }),
    ]
    # Для литерального -i адреса важно, чтобы host playbook был 'nas'.
    # --limit не требуется: динамическая группа создаётся отдельным
    # инвентарём в следующей функции.
    inventory = repo_root / "infrastructure" / "hosts" / target.name / "ansible-inventory.yml"
    if not inventory.is_file():
        raise InfraManagerError(f"Отсутствует Ansible-инвентарь NAS: {inventory}")
    args[2:4] = ["-i", str(inventory)]
    console.info("Применение базовых настроек Debian/OMV через Ansible")
    result = run(args, env=environment, stream_output=True, check=False)
    if result.returncode:
        raise InfraManagerError(
            "Ansible не смог настроить NAS. Хранилища не изменялись; "
            "проверьте причину выше и повторите deploy nas"
        )
