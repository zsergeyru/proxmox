"""Generic Ansible executor for declarative physical NAS provisioning.

No application-specific deployment scripts or hardcoded playbook sequence:
one role, two guarded phases and a checked provision.yaml contract.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

from .common import InfraManagerError, console, run
from .physical_host_ssh import PhysicalHostSshSession


def load_nas_provision(repo_root: Path) -> dict:
    path = repo_root / "infrastructure" / "hosts" / "nas" / "provision.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError("Не удалось прочитать декларативное описание NAS") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "host", "system", "deployment", "services"
    }:
        raise InfraManagerError("Неподдерживаемый формат provision.yaml физического NAS")
    if raw["schema_version"] != 1 or raw["host"] != "nas":
        raise InfraManagerError("Не совпадает физический сервер provision.yaml")
    plan = raw.get("deployment")
    if not isinstance(plan, dict) or set(plan) != {"phases", "components"}:
        raise InfraManagerError("Не определены безопасные этапы NAS")
    if plan["phases"] != ["base", "services"] or plan["components"] != [
        "urbackup_server", "urbackup_client", "apps", "smb"
    ]:
        raise InfraManagerError("Неизвестные компоненты или порядок развёртывания NAS")
    services = raw.get("services")
    if not isinstance(services, dict) or set(services) != {
        "openmediavault", "qbittorrent", "urbackup"
    }:
        raise InfraManagerError("Неизвестный список сервисов в provision.yaml NAS")
    qbit = services["qbittorrent"]
    if not isinstance(qbit, dict) or not re.fullmatch(
        r"lscr\.io/linuxserver/qbittorrent:[A-Za-z0-9_.-]+",
        str(qbit.get("image", "")),
    ):
        raise InfraManagerError("Некорректный образ qBittorrent")
    if qbit.get("url") != "http://{address}:8080/":
        raise InfraManagerError("Адрес qBittorrent не соответствует опубликованному порту")
    urbackup = services["urbackup"]
    if not isinstance(urbackup, dict) or urbackup.get("url") != "http://127.0.0.1:55414/":
        raise InfraManagerError("Описание UrBackup не соответствует защищённому bind localhost")
    return raw


def run_nas_phase(
    repo_root: Path,
    session: PhysicalHostSshSession,
    phase: str,
    *,
    provision: dict,
    policy: dict,
    verified_volumes: dict[str, object] | None = None,
    host_volumes: dict[str, dict] | None = None,
) -> None:
    """Run one declarative NAS role phase over certificate-authenticated SSH."""
    if phase not in ("base", "services"):
        raise InfraManagerError("Неизвестный этап декларативного deploy NAS")
    if phase == "services" and (not verified_volumes or not host_volumes):
        raise InfraManagerError("Нельзя разворачивать сервисы без проверки физических томов")
    if not session.host.address or session.host.name != provision["host"]:
        raise InfraManagerError("Неподтверждённый адрес физического NAS")
    playbook = repo_root / "automation" / "ansible" / "playbooks" / "deploy-nas.yml"
    inventory = repo_root / "infrastructure" / "hosts" / "nas" / "ansible-inventory.yml"
    if not playbook.is_file() or not inventory.is_file():
        raise InfraManagerError("Нет декларативного playbook или inventory NAS")

    variables: dict[str, object] = {
        "nas_deploy_phase": phase,
        "nas_provision": provision,
        "nas_backup_policy": policy,
    }
    if phase == "services":
        assert verified_volumes is not None and host_volumes is not None
        shared = verified_volumes["shared"]
        data = verified_volumes["data"]
        backup = verified_volumes["backup"]
        variables.update({
            "nas_shared_mount": shared.mountpoint,
            "nas_shared_uuid": host_volumes["shared"]["filesystem_uuid"],
            "nas_shared_ref": host_volumes["shared"]["omv_reference"],
            "nas_data_pool": host_volumes["data"]["pool"],
            "nas_data_guid": host_volumes["data"]["pool_guid"],
            "nas_data_mountpoint": data.mountpoint,
            "nas_data_ref": host_volumes["data"]["omv_reference"],
            "nas_backup_pool": host_volumes["backup"]["pool"],
            "nas_backup_guid": host_volumes["backup"]["pool_guid"],
            "nas_backup_mountpoint": backup.mountpoint,
        })
    key, ssh_args = session.ansible_parameters()
    variables.update({
        "ansible_host": session.host.address,
        "ansible_ssh_common_args": ssh_args,
        "ansible_python_interpreter": "/usr/bin/python3",
    })
    env = os.environ.copy()
    env.update({
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOG_PATH": "/dev/null",
        "ANSIBLE_SSH_TRANSFER_METHOD": "piped",
        "ANSIBLE_STDOUT_CALLBACK": "infra_nas_compact",
        "ANSIBLE_CALLBACK_PLUGINS": str(
            repo_root / "automation" / "ansible" / "callback_plugins"
        ),
        "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "False",
        "ANSIBLE_DEPRECATION_WARNINGS": "False",
        "ANSIBLE_NOCOLOR": "1",
    })
    console.info("Применение конфигурации NAS: deploy-nas.yml (" + phase + ")")
    result = run([
        "ansible-playbook", str(playbook), "-i", str(inventory),
        "-u", session.host.user, "--private-key", key,
        "-e", json.dumps(variables),
    ], env=env, cwd=repo_root, stream_output=True, check=False)
    if result.returncode:
        raise InfraManagerError(
            f"Декларативный Ansible NAS ({phase}) завершился с ошибкой "
            f"({result.returncode}); диски не форматировались"
        )
