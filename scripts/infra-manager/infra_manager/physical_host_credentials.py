"""Generic physical-host operator credential handling.

The host's operator.yaml owns prompts, status presentation and credential IDs.
No application name, concrete host, or account policy is embedded in this module.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from string import Formatter

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError
from .settings import PATHS


_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def operator_declarations(repo_root: Path, host: str) -> dict:
    """Read and validate an optional host-local operator contract."""
    if not _NAME.fullmatch(host):
        raise InfraManagerError("Некорректное имя физического сервера")
    manifest = repo_root / "infrastructure" / "hosts" / host / "operator.yaml"
    if not manifest.exists():
        return {"host": host, "credentials": []}
    schema = repo_root / "infrastructure" / "schemas" / "physical-host-operator.schema.yaml"
    try:
        document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        rules = yaml.safe_load(schema.read_text(encoding="utf-8"))
        issues = list(Draft202012Validator(rules).iter_errors(document))
    except (OSError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise InfraManagerError("Не удалось загрузить описание операторских данных") from exc
    if issues or document["host"] != host:
        raise InfraManagerError("Неверная декларация операторских данных физического сервера")
    credentials = document["credentials"]
    ids = [entry["id"] for entry in credentials]
    if len(set(ids)) != len(ids):
        raise InfraManagerError("Повторяются идентификаторы операторских секретов")
    for entry in credentials:
        if (entry["min_length"] > entry["max_length"]
                or any(ord(c) < 32 or ord(c) == 127 for c in entry["prompt"])):
            raise InfraManagerError("Некорректные параметры запроса секрета")
        template = entry["status"]["share_url"]
        parts = list(Formatter().parse(template))
        if not all(field is None or field in ("address", "name")
                   for _, field, _, _ in parts):
            raise InfraManagerError("Неизвестные параметры адреса в статусе")
        if not all(not format_spec and conversion is None
                   for _, _, format_spec, conversion in parts):
            raise InfraManagerError("Форматирование статуса ограничено простыми полями")
    return document


def _entry(repo_root: Path, host: str, credential_id: str) -> dict:
    """Require an explicitly declared credential, never accept arbitrary paths."""
    document = operator_declarations(repo_root, host)
    matches = [spec for spec in document["credentials"] if spec["id"] == credential_id]
    if len(matches) != 1:
        raise InfraManagerError("Секрет не объявлен в операторской конфигурации")
    return matches[0]


def _secret_path(host: str, credential_id: str, *, root: Path | None = None) -> Path:
    """Resolve only controlled filenames in the persistent physical-host store."""
    if not _NAME.fullmatch(host) or not _NAME.fullmatch(credential_id):
        raise InfraManagerError("Недопустимый идентификатор секрета")
    root = root if root is not None else PATHS.data_dir / "physical-hosts"
    if not root.is_dir():
        raise InfraManagerError("Не подключено постоянное состояние физических серверов")
    return root / f"{host}-{credential_id}-password"


def validate_secret(password: str, spec: dict) -> None:
    if (not isinstance(password, str)
            or not spec["min_length"] <= len(password) <= spec["max_length"]
            or any(c in password for c in ("\\n", "\\r", "\\x00"))):
        raise InfraManagerError("Пароль не соответствует требованиям объявления")


def save_operator_secret(
    repo_root: Path, host: str, credential_id: str, password: str, *,
    state_root: Path | None = None,
) -> None:
    """Atomically write a private secret; do not log or return its content."""
    spec = _entry(repo_root, host, credential_id)
    validate_secret(password, spec)
    path = _secret_path(host, credential_id, root=state_root)
    if path.is_symlink():
        raise InfraManagerError("Вместо секрета обнаружена символьная ссылка")
    fd, temp = tempfile.mkstemp(prefix=f".{host}-{credential_id}-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        # Runtime executes as UID 1001, controller as root. No world/group read.
        os.fchown(fd, 1001, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(password + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def read_operator_secret(
    repo_root: Path, host: str, credential_id: str, *,
    state_root: Path | None = None,
) -> str:
    """Read a saved secret, validating mode and its configured length."""
    spec = _entry(repo_root, host, credential_id)
    path = _secret_path(host, credential_id, root=state_root)
    if path.is_symlink() or not path.is_file():
        raise InfraManagerError("Операторский пароль ещё не задан")
    try:
        metadata = path.stat()
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InfraManagerError("Не удалось прочитать защищённый пароль") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise InfraManagerError("Некорректные права доступа к паролю")
    password = value.removesuffix("\n")
    validate_secret(password, spec)
    return password


def credential_plan(repo_root: Path, host: str) -> dict:
    """Publish only prompt metadata; never send the stored values."""
    document = operator_declarations(repo_root, host)
    return {"host": host, "credentials": [
        {k: spec[k] for k in ("id", "prompt", "min_length", "max_length")}
        for spec in document["credentials"]
    ]}


def operator_status_sections(
    repo_root: Path, host: str, address: str, *,
    show_secrets: bool,
) -> None:
    """Present host-declared account and share information without service logic."""
    document = operator_declarations(repo_root, host)
    for spec in document["credentials"]:
        status = spec["status"]
        print("\n" + status["title"])
        if status["accounts_source"] == "global_users":
            from .user_catalog import load_user_catalog
            people = load_user_catalog(repo_root)["users"]
            print("  Логины:      " + ", ".join(item["name"] for item in people))
        password = "скрыт (доступен в доверенном status на PVE)"
        if show_secrets:
            try:
                password = read_operator_secret(repo_root, host, spec["id"])
            except InfraManagerError:
                password = "не задан или недоступен"
        print("  Пароль:      " + password)
        print("  Права:       " + status["permissions"])
        if status["shares_source"] == "provision":
            manifest = repo_root / "infrastructure" / "hosts" / host / "provision.yaml"
            try:
                provision = yaml.safe_load(manifest.read_text(encoding="utf-8"))
                names = [item["name"] for item in provision["shares"]]
            except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
                raise InfraManagerError("Нельзя прочитать ресурсы физической машины") from exc
            for name in names:
                print("  Папка:       " + status["share_url"].format(address=address, name=name))
