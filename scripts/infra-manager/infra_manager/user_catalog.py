"""Первоначальный каталог людей и групп проекта без паролей.

Каталог не является базой учётных данных приложений. Изменения в Authelia
и других службах сохраняются в их постоянных хранилищах.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError

_RESERVED_USERS = frozenset({
    "admin", "root", "nobody", "daemon", "bin", "sys", "sync",
    "games", "man", "lp", "mail", "news", "uucp", "proxy",
    "www-data", "backup", "list", "irc", "gnats",
    "systemd-network", "systemd-timesync", "messagebus",
    "sshd", "docker", "sudo", "nas-qbittorrent",
})
_RESERVED_GROUPS = frozenset({
    "root", "nobody", "daemon", "bin", "sys", "adm",
    "disk", "tty", "dialout", "sudo", "users", "docker",
    "www-data", "systemd-journal", "ssh", "staff",
})


def validate_user_catalog(repo_root: Path, document: object) -> dict[str, object]:
    """Проверить формат, уникальность и взаимосвязи имён без секретов."""

    schema_path = repo_root / "infrastructure" / "schemas" / "users.schema.yaml"
    try:
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: str(error.path),
        )
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise InfraManagerError("Не удалось проверить схему общего каталога") from exc
    if errors:
        error = errors[0]
        field = ".".join(map(str, error.path)) or "users.yaml"
        raise InfraManagerError(f"infrastructure/users.yaml: {field}: {error.message}")

    assert isinstance(document, dict)
    groups = document["groups"]
    users = document["users"]
    defined = set(groups)
    if len(defined) != len(groups):
        raise InfraManagerError("Повторяющиеся группы общего каталога")
    if defined & _RESERVED_GROUPS:
        raise InfraManagerError("Общая группа совпадает с системной группой Linux")
    if "admins" not in defined:
        raise InfraManagerError("Группа admins обязательна для начального администратора Authelia")

    usernames: set[str] = set()
    for item in users:
        name = item["name"]
        if name in usernames or name in _RESERVED_USERS:
            raise InfraManagerError(f"Повторяющееся либо системное имя пользователя: {name}")
        usernames.add(name)
        if not set(item["groups"]).issubset(defined):
            raise InfraManagerError(f"Пользователь {name} ссылается на неизвестную группу")
    return document


def load_user_catalog(repo_root: Path) -> dict[str, object]:
    """Прочитать единый начальный перечень из публичного проекта."""

    path = repo_root / "infrastructure" / "users.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InfraManagerError("Не найден или повреждён infrastructure/users.yaml") from exc
    return validate_user_catalog(repo_root, document)
