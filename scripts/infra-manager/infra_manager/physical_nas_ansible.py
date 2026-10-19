"""Применение Ansible к физическому NAS без VMID, OpenTofu и Semaphore."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .common import InfraManagerError, run, console
from .physical_host_ssh import PhysicalHostSshSession


def run_nas_playbook(
    repo_root: Path, session: PhysicalHostSshSession,
    name: str, variables: dict[str, object] | None = None,
) -> None:
    """Запустить разрешённый сценарий с теми же временными SSH-полномочиями."""

    if name not in {"configure-nas.yml", "configure-nas-apps.yml", "configure-nas-backup.yml", "configure-nas-smb.yml"}:
        raise InfraManagerError("Неизвестный сценарий настройки NAS")
    target = session.host
    if not target.address:
        raise InfraManagerError("Не задан адрес физического NAS")
    playbook = repo_root / "automation" / "ansible" / "playbooks" / name
    inventory = (
        repo_root / "infrastructure" / "hosts" / target.name
        / "ansible-inventory.yml"
    )
    if not playbook.is_file() or not inventory.is_file():
        raise InfraManagerError("Не найдены Ansible-сценарий или инвентарь NAS")
    key, ssh_args = session.ansible_parameters()
    environment = os.environ.copy()
    environment.update({
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOG_PATH": "",
    })
    options = {
        "ansible_host": target.address,
        "ansible_ssh_common_args": ssh_args,
        **(variables or {}),
    }
    args = [
        "ansible-playbook", str(playbook),
        "-i", str(inventory),
        "-u", target.user,
        "--private-key", key,
        "-e", json.dumps(options),
    ]
    console.info(f"Применение конфигурации NAS: {name}")
    result = run(
        args, env=environment, cwd=repo_root,
        stream_output=True, check=False,
    )
    if result.returncode:
        raise InfraManagerError(
            f"Сценарий NAS {name} завершился с ошибкой "
            f"({result.returncode}). Диски не форматировались"
        )


def configure_nas(repo_root: Path, session: PhysicalHostSshSession) -> None:
    """Установить базовые программы Debian/OMV, не изменяя хранилища."""

    run_nas_playbook(repo_root, session, "configure-nas.yml")


def configure_nas_apps(
    repo_root: Path, session: PhysicalHostSshSession,
    *, shared_mount: str, shared_uuid: str,
) -> None:
    """Настроить qBittorrent после подтверждения UUID HDD и OMV."""

    run_nas_playbook(repo_root, session, "configure-nas-apps.yml", {
        "nas_shared_mount": shared_mount,
        "nas_shared_uuid": shared_uuid,
        "nas_qbit_image": "lscr.io/linuxserver/qbittorrent:5.2.4_v2.0.15-ls479",
    })


def configure_nas_backups(
    repo_root: Path, session: PhysicalHostSshSession,
    *, source_guid: str, backup_guid: str,
    data_mountpoint: str, backup_mountpoint: str,
) -> None:
    """Включить независимую передачу ZFS без удаления старых копий."""

    run_nas_playbook(repo_root, session, "configure-nas-backup.yml", {
        "nas_data_pool": "nas-data",
        "nas_backup_pool": "nas-backup",
        "nas_data_guid": source_guid,
        "nas_backup_guid": backup_guid,
        "nas_data_mountpoint": data_mountpoint,
        "nas_backup_mountpoint": backup_mountpoint,
    })


def configure_nas_smb(
    repo_root: Path, session: PhysicalHostSshSession,
    *, shared_mount: str, shared_uuid: str, shared_ref: str,
    data_mountpoint: str, data_ref: str,
) -> None:
    """Создать защищённые общие папки штатной базой OMV, без smb.conf."""

    run_nas_playbook(repo_root, session, "configure-nas-smb.yml", {
        "nas_shared_mount": shared_mount,
        "nas_shared_uuid": shared_uuid,
        "nas_shared_ref": shared_ref,
        "nas_data_mountpoint": data_mountpoint,
        "nas_data_ref": data_ref,
    })
