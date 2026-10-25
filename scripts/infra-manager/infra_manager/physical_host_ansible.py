"""Universal, allowlisted Ansible invocation for a physical host.

Owns only Git-managed playbook selection and temporary OpenBao SSH transport.
Host-specific facts, disk policy and services belong to the Ansible playbook.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

from .common import InfraManagerError, console, run
from .physical_host_ssh import PhysicalHostSshSession


_HOST = re.compile(r"^[a-z][a-z0-9-]*$")
_CALLBACK = re.compile(r"^[a-z][a-z0-9_]*$")


def run_physical_ansible(
    repo_root: Path,
    session: PhysicalHostSshSession,
    *,
    variables: Mapping[str, object],
    phase: str | None = None,
    operation: str = "deploy",
    callback: str | None = None,
) -> None:
    """Execute the checked deployment playbook using the trusted SSH CA.

    No playbook path, inventory path or shell command comes from a YAML value.
    Extra-vars are serialized as JSON, never interpolated into a shell.
    """
    name = session.host.name
    if not _HOST.fullmatch(name) or not session.host.address:
        raise InfraManagerError("Недопустимая или неподтверждённая физическая машина")
    if operation == "deploy" and phase in ("base", "services"):
        playbook_name = f"deploy-{name}.yml"
    elif operation == "inspect" and phase is None:
        playbook_name = "inspect-physical-storage.yml"
    else:
        raise InfraManagerError("Недопустимый Ansible playbook или этап")
    playbook = repo_root / "automation" / "ansible" / "playbooks" / playbook_name
    inventory = repo_root / "infrastructure" / "hosts" / name / "ansible-inventory.yml"
    if not playbook.is_file() or playbook.is_symlink():
        raise InfraManagerError("Нет проверенного Ansible playbook физического сервера")
    if not inventory.is_file() or inventory.is_symlink():
        raise InfraManagerError("Нет проверенного Ansible inventory физического сервера")
    if callback is not None:
        if not _CALLBACK.fullmatch(callback):
            raise InfraManagerError("Недопустимый callback Ansible")
        plugin = repo_root / "automation" / "ansible" / "callback_plugins" / (callback + ".py")
        if not plugin.is_file() or plugin.is_symlink():
            raise InfraManagerError("Не найден объявленный callback Ansible")
    private_key, ssh_args = session.ansible_parameters()
    data = dict(variables)
    # SSH target is set from the verified session, never caller supplied YAML.
    data.update({
        "physical_host_name": name,
        "ansible_host": session.host.address,
        "ansible_ssh_common_args": ssh_args,
        "ansible_python_interpreter": "/usr/bin/python3",
    })
    environment = os.environ.copy()
    environment.update({
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOG_PATH": "/dev/null",
        "ANSIBLE_SSH_TRANSFER_METHOD": "piped",
        "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "False",
        "ANSIBLE_DEPRECATION_WARNINGS": "False",
        "ANSIBLE_NOCOLOR": "1",
    })
    if callback:
        environment.update({
            "ANSIBLE_STDOUT_CALLBACK": callback,
            "ANSIBLE_CALLBACK_PLUGINS": str(
                repo_root / "automation" / "ansible" / "callback_plugins"
            ),
        })
    console.info(f"Ansible {name}: {operation}" + (f" ({phase})" if phase else ""))
    result = run(
        ["ansible-playbook", str(playbook), "-i", str(inventory),
         "-u", session.host.user, "--private-key", private_key,
         "-e", json.dumps(data)],
        env=environment, cwd=repo_root, stream_output=True, check=False,
    )
    if result.returncode:
        raise InfraManagerError(
            f"Ansible физического сервера ({operation}) остановился "
            f"с кодом {result.returncode}; результат не подтверждён"
        )
