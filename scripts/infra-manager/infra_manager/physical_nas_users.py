"""Доступ NAS по SMB: общий каталог людей и локальные разрешения папок.

Имена и группы объявлены единожды в infrastructure/users.yaml.
SMB включён без паролей в гостевом режиме; доступ задан в access.yaml.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError
from .user_catalog import load_user_catalog
from .physical_nas_ansible import load_nas_provision


def validate_nas_access(
    repo_root: Path, document: object,
    catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    """Проверить объявленный гостевой доступ ко всем SMB-папкам."""

    if catalog is None:
        catalog = load_user_catalog(repo_root)
    schema_path = repo_root / "infrastructure" / "schemas" / "nas-access.schema.yaml"
    try:
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: str(error.path),
        )
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise InfraManagerError("Не удалось проверить схему прав NAS") from exc
    if errors:
        error = errors[0]
        field = ".".join(map(str, error.path)) or "access.yaml"
        raise InfraManagerError(f"NAS access.yaml: {field}: {error.message}")
    assert isinstance(document, dict)
    declared = {share["name"] for share in load_nas_provision(repo_root)["shares"]}
    if set(document["shares"]) != declared:
        raise InfraManagerError(
            "Назначения access.yaml не совпадают с папками provision.yaml"
        )
    # Guest-only resources deliberately don't authenticate named users.
    # A password or per-user permission block fails the strict JSON schema.
    return document


def load_nas_users(repo_root: Path) -> dict[str, object]:
    """Собрать начальные имена и права из двух непересекающихся источников."""

    catalog = load_user_catalog(repo_root)
    path = repo_root / "infrastructure" / "hosts" / "nas" / "access.yaml"
    try:
        rights = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InfraManagerError("Не найден или повреждён NAS access.yaml") from exc
    validate_nas_access(repo_root, rights, catalog)
    return {
        "schema_version": rights["schema_version"],
        "mode": rights["mode"],
        "users": catalog["users"],
        "groups": catalog["groups"],
        "shares": rights["shares"],
    }


def require_nas_users_configured(repo_root: Path) -> dict[str, object]:
    """Не считать SMB готовым при отсутствии согласованных людей и прав."""

    document = load_nas_users(repo_root)
    if not document["shares"] or any(
        rights["access"] != "read_write"
        for rights in document["shares"].values()
    ):
        raise InfraManagerError("Все SMB-папки NAS должны быть доступны гостю на запись")
    return document
