"""Штатное подключение подтверждённого NAS NFSv4 через Proxmox API.

Никаких `mount`/fstab/Apt скриптов на хост не устанавливается. Источник
берётся только из проверенного backup.yaml; при его отсутствии ничего не меняем.
"""

from __future__ import annotations

import re
from pathlib import Path
import yaml

from .common import InfraManagerError, console, run
from .pve import PveClient
from .pve_host import _ssh_command
from .settings import PATHS

STORAGE_ID = "nas-backup"
MOUNTPOINT = "/mnt/pve/nas-backup"
SOURCE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9.:-]{0,252}):(/[-A-Za-z0-9_./]+)$")


def _declared() -> tuple[str, str] | None:
    manifest = PATHS.repo_root / "infrastructure/hosts/pve/backup.yaml"
    if manifest.is_symlink() or not manifest.is_file():
        raise InfraManagerError("Нет доверенного backup.yaml PVE")
    policy = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise InfraManagerError("Неверный backup.yaml PVE")
    entries = policy.get("destinations")
    matching = [v for v in entries if isinstance(v, dict)
                and v.get("id") == STORAGE_ID] if isinstance(entries, list) else []
    if len(matching) != 1 or matching[0].get("mountpoint") != MOUNTPOINT:
        raise InfraManagerError("Неоднозначное назначение NAS в backup.yaml")
    source = matching[0].get("source")
    if source in (None, ""):
        return None
    if not isinstance(source, str) or not (match := SOURCE.fullmatch(source)):
        raise InfraManagerError("Неверный NFSv4 источник NAS")
    server, export = match.groups()
    if ".." in Path(export).parts or "//" in export or export == "/":
        raise InfraManagerError("Недопустимый NFS экспорт NAS")
    return server, export


def _mount_verified(node: str, server: str, export: str) -> bool:
    result = run(
        _ssh_command(node, ("findmnt", "-n", "-M", MOUNTPOINT, "-o", "SOURCE,FSTYPE")),
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return False
    parts = result.stdout.split()
    return len(parts) == 2 and parts == [f"{server}:{export}", "nfs4"]


def reconcile_backup_nas(client: PveClient, node: str) -> bool:
    """Только разрешённый NFS экспорт; не перезаписывать чужое storage."""
    declared = _declared()
    if declared is None:
        return False
    server, export = declared
    storages = client.data("/storage")
    if not isinstance(storages, list):
        raise InfraManagerError("PVE не подтвердил перечень storage")
    matches = [s for s in storages if isinstance(s, dict)
               and s.get("storage") == STORAGE_ID]
    if len(matches) > 1:
        raise InfraManagerError("Дублируется NAS storage")
    if matches:
        actual = client.data(f"/storage/{STORAGE_ID}")
        if not isinstance(actual, dict) or any((
            actual.get("type") != "nfs",
            actual.get("server") != server,
            actual.get("export") != export,
            actual.get("options") not in ("vers=4.2", "vers=4", "vers=4.1"),
        )):
            raise InfraManagerError("NAS storage уже занят другой конфигурацией")
    else:
        client.request_data("POST", "/storage", form={
            "type": "nfs", "storage": STORAGE_ID, "server": server,
            "export": export, "content": "backup", "nodes": node,
            "options": "vers=4.2",
        })
    if not _mount_verified(node, server, export):
        raise InfraManagerError("NAS NFSv4 не смонтирован; резервирование ожидает доступности")
    return True


def deploy_backup_nas_preflight() -> None:
    """Подключение NAS не делает необязательный для 910 резервный диск блокером."""
    from .operator import _pve_node
    try:
        if reconcile_backup_nas(PveClient(), _pve_node()):
            console.ok("NAS NFSv4 подключён штатным Proxmox API")
        else:
            console.info("NAS ещё не указан в backup.yaml; копии возможны только на SSD")
    except InfraManagerError as exc:
        console.warning(f"NAS для резервирования: {exc}. Самообновление 910 продолжится")
