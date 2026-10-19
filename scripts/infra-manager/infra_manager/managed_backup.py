"""Проверка готовности управляемого резервирования из 910.

PVE выполняет только защищённые операции существующего backup-export.py;
эта стадия не создаёт архивов и не устанавливает пакетов на PVE.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import yaml

from .common import InfraManagerError
from .pve_host import _ssh_command
from .settings import PATHS

CONFIG = PATHS.repo_root / "infrastructure" / "hosts" / "pve" / "backup.yaml"
HELPER = "/usr/local/lib/infra-manager/backup/backup-export.py"
EXPECTED_MOUNTS = {
    "fast-ssd": "/mnt/pve/backup-fast",
    "nas-backup": "/mnt/pve/nas-backup",
}


def _targets(config: dict) -> dict[str, str]:
    """Не допускать запись при неопределённой идентичности носителей."""
    if config.get("schema_version") != 1 or config.get("job") != "infra-core":
        raise InfraManagerError("Некорректное описание резервирования")
    destinations = config.get("destinations")
    if not isinstance(destinations, list) or len(destinations) != 2:
        raise InfraManagerError("Не определены SSD и NAS для резервирования")
    result: dict[str, str] = {}
    for item in destinations:
        if not isinstance(item, dict) or item.get("id") not in EXPECTED_MOUNTS:
            raise InfraManagerError("Неизвестное место хранения копии")
        name = item["id"]
        if name in result or item.get("mountpoint") != EXPECTED_MOUNTS[name]:
            raise InfraManagerError("Недопустимое назначение резервирования")
        identity = item.get("uuid") or item.get("source")
        if not isinstance(identity, str) or not identity.strip():
            raise InfraManagerError(
                f"Носитель {name} ещё не подтверждён в backup.yaml; "
                "никаких архивов не создано"
            )
        result[name] = identity.strip()
    if set(result) != set(EXPECTED_MOUNTS) or len(set(result.values())) != 2:
        raise InfraManagerError("Два независимых носителя не подтверждены")
    return result


def check_backup_ready(node: str, config_path: Path = CONFIG) -> str:
    """Проверить идентичность и PVE-only готовность без запуска копирования."""
    if config_path.is_symlink() or not config_path.is_file():
        raise InfraManagerError("Не найдено безопасное описание backup.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise InfraManagerError("Описание backup.yaml должно быть объектом")
    targets = _targets(config)
    if not node or any(char.isspace() for char in node):
        raise InfraManagerError("Неизвестен узел PVE")
    argv = _ssh_command(node, ("python3", HELPER, "check"))
    result = subprocess.run(
        argv,
        input=json.dumps(targets),
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if result.returncode:
        raise InfraManagerError(
            "PVE не подтвердил носители или закрытое состояние; "
            "резервирование не запускается"
        )
    if result.stdout.strip() != "[ОК] Источники и физические носители подтверждены":
        raise InfraManagerError("Неподтверждённый ответ узкого помощника PVE")
    return "PVE, SSD и NAS подтверждены; резервные копии ещё не создавались"
