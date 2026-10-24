"""Строгое чтение политики NAS; никакой подстановки физических устройств."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError


def load_nas_backup_policy(repo_root: Path) -> dict[str, object]:
    path = repo_root / "infrastructure/hosts/nas/backup.yaml"
    schema_path = repo_root / "infrastructure/schemas/nas-backup.schema.yaml"
    try:
        policy = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(policy),
                        key=lambda e: ".".join(map(str, e.absolute_path)))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise InfraManagerError("Невозможно прочитать политику резервирования NAS") from exc
    if errors:
        field = ".".join(map(str, errors[0].absolute_path)) or "backup.yaml"
        raise InfraManagerError(f"NAS backup.yaml: {field}: {errors[0].message}")
    assert isinstance(policy, dict)
    image = policy["server"]["image"]
    if ":latest" in image or (":2.5.x" not in image and not re.search(
        r"@sha256:[0-9a-f]{64}$", image
    )):
        raise InfraManagerError("Образ UrBackup должен быть известной 2.5.x или закреплённым digest")
    server = policy["server"]
    if server["data_subdir"] == server["recovery_subdir"]:
        raise InfraManagerError("Данные и аварийная копия UrBackup должны храниться отдельно")
    return policy


def verify_nas_backup_volumes(policy: dict[str, object], volumes: tuple) -> tuple[str, str]:
    """Проверять роли через штатный verify_nas_volumes; не принимать обычные папки."""
    by_role = {v.role: v for v in volumes}
    if set(by_role) != {"shared", "data", "backup"}:
        raise InfraManagerError("Невозможно подтвердить все диски NAS")
    source = by_role[policy["local_client"]["source_volume"]]
    target = by_role[policy["local_client"]["target_volume"]]
    if (source.mountpoint == target.mountpoint or
            source.source.split("/", 1)[0] != "nas-data" or
            target.source.split("/", 1)[0] != "nas-backup" or
            source.filesystem != "zfs" or target.filesystem != "zfs"):
        raise InfraManagerError("Неверные источники резервирования NAS")
    return source.mountpoint, target.mountpoint
