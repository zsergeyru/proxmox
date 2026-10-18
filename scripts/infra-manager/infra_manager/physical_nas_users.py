"""Проверка списка учётных записей NAS без чтения или хранения паролей.

Файл users.yaml является источником только имён, групп и прав SMB.
Пароли должны выдаваться отдельно через OpenBao.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError

_RESERVED = frozenset({
    "root", "admin", "nobody", "daemon", "bin", "sys", "sync",
    "games", "man", "lp", "mail", "news", "uucp", "proxy",
    "www-data", "backup", "list", "irc", "gnats",
    "systemd-network", "systemd-timesync", "messagebus",
    "sshd", "docker", "sudo", "users", "adm", "nas-qbittorrent",
})
_SHARE_NAMES = ("shared", "movies", "music", "data")


def validate_nas_users(repo_root: Path, document: object) -> dict[str, object]:
    """Проверить синтаксис, отсутствие секретов и ссылки на группы."""

    schema_path = repo_root / "infrastructure" / "schemas" / "nas-users.schema.yaml"
    try:
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(document), key=lambda error: str(error.path))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise InfraManagerError("Не удалось прочитать схему пользователей NAS") from exc
    if errors:
        error = errors[0]
        field = ".".join(map(str, error.path)) or "users.yaml"
        raise InfraManagerError(f"Пользователи NAS: {field}: {error.message}")
    assert isinstance(document, dict)

    groups = document["groups"]
    users = document["users"]
    shares = document["shares"]
    group_names = set(groups)
    if len(group_names) != len(groups):
        raise InfraManagerError("Повторяющиеся группы в users.yaml")
    if group_names & _RESERVED:
        raise InfraManagerError(
            "Группа NAS совпадает с существующим системным именем"
        )

    seen: set[str] = set()
    for user in users:
        name = user["name"]
        if name in seen or name in _RESERVED:
            raise InfraManagerError(
                f"Повторяющееся или системное имя пользователя NAS: {name}"
            )
        seen.add(name)
        if not set(user["groups"]) <= group_names:
            raise InfraManagerError(
                f"Пользователь {name} ссылается на неизвестную группу"
            )

    for share in _SHARE_NAMES:
        entry = shares[share]
        unknown = (
            set(entry["read_groups"]) | set(entry["write_groups"])
        ) - group_names
        if unknown:
            raise InfraManagerError(
                f"Папка {share}: неизвестные группы доступа: {sorted(unknown)}"
            )
    return document


def load_nas_users(repo_root: Path) -> dict[str, object]:
    """Загрузить машинное описание пользователей до развёртывания."""

    path = repo_root / "infrastructure" / "hosts" / "nas" / "users.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InfraManagerError("Нельзя прочитать infrastructure/hosts/nas/users.yaml") from exc
    return validate_nas_users(repo_root, document)


def require_nas_users_configured(repo_root: Path) -> dict[str, object]:
    """Не позволить объявить пользователей NAS настроенными при пустом файле."""

    document = load_nas_users(repo_root)
    if not document["users"]:
        raise InfraManagerError(
            "Сначала укажите пользователей NAS в infrastructure/hosts/nas/users.yaml"
        )
    if not any(
        document["shares"][share]["read_groups"]
        or document["shares"][share]["write_groups"]
        for share in _SHARE_NAMES
    ):
        raise InfraManagerError(
            "В users.yaml ещё не назначены права ни на одну папку SMB"
        )
    return document
