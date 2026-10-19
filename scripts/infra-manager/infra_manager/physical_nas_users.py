"""Доступ NAS по SMB: общий каталог людей и локальные разрешения папок.

Имена и группы объявлены единожды в infrastructure/users.yaml.
Права четырёх сетевых папок задаёт только NAS в access.yaml.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError
from .user_catalog import load_user_catalog

_SHARE_NAMES = ("shared", "movies", "music", "data")


def validate_nas_access(
    repo_root: Path, document: object,
    catalog: dict[str, object] | None = None,
) -> dict[str, object]:
    """Проверить права только на группы из общего каталога."""

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
    known_groups = set(catalog["groups"])
    for share in _SHARE_NAMES:
        rights = document["shares"][share]
        unknown = (
            set(rights["read_groups"]) | set(rights["write_groups"])
        ) - known_groups
        if unknown:
            raise InfraManagerError(
                f"Папка NAS {share}: неизвестные общие группы: {sorted(unknown)}"
            )
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
        "schema_version": 1,
        "users": catalog["users"],
        "groups": catalog["groups"],
        "shares": rights["shares"],
    }


def require_nas_users_configured(repo_root: Path) -> dict[str, object]:
    """Не считать SMB готовым при отсутствии согласованных людей и прав."""

    document = load_nas_users(repo_root)
    if not document["users"]:
        raise InfraManagerError(
            "Укажите пользователей в infrastructure/users.yaml до настройки SMB"
        )
    if not any(
        document["shares"][share]["read_groups"]
        or document["shares"][share]["write_groups"]
        for share in _SHARE_NAMES
    ):
        raise InfraManagerError(
            "Назначьте права в infrastructure/hosts/nas/access.yaml"
        )
    return document
