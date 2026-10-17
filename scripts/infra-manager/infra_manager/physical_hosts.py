"""Каталог физических машин, независимых от PVE VMID и OpenTofu."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError


_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class PhysicalHost:
    """Проверенное машинное описание физического сервера."""

    name: str
    address: str | None
    port: int
    user: str
    ssh_host_fingerprint: str | None
    devices: tuple[dict[str, object], ...]
    directory: Path


def load_physical_host(repo_root: Path, name: str) -> PhysicalHost:
    """Прочитать host.yaml, проверить схему и не допустить обход каталога."""

    if not _NAME.fullmatch(name):
        raise InfraManagerError(f"Некорректное имя физического сервера: {name!r}")

    directory = repo_root / "infrastructure" / "hosts" / name
    manifest = directory / "host.yaml"
    schema_file = repo_root / "infrastructure" / "schemas" / "physical-host.schema.yaml"
    if not manifest.is_file():
        raise InfraManagerError(f"Нет машинного описания физического сервера: {manifest}")
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        schema = yaml.safe_load(schema_file.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise InfraManagerError(f"Не удалось прочитать описание сервера {name}") from exc
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda err: str(err.path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.path) or "корень"
        raise InfraManagerError(
            f"{manifest}: ошибка поля {location}: {error.message}"
        )
    if data["name"] != name:
        raise InfraManagerError(f"{manifest}: name не совпадает с каталогом {name}")
    roles = [device["role"] for device in data["storage"]["devices"]]
    if roles.count("system") != 1:
        raise InfraManagerError(f"{manifest}: нужен ровно один системный накопитель")
    for role in ("shared", "data", "backup"):
        if roles.count(role) > 1:
            raise InfraManagerError(
                f"{manifest}: роль накопителя {role} не может повторяться"
            )
    serials = [device["serial"] for device in data["storage"]["devices"] if device["serial"]]
    if len(serials) != len(set(serials)):
        raise InfraManagerError(f"{manifest}: повторяются серийные номера дисков")
    conn = data["connection"]
    return PhysicalHost(
        name=name,
        address=conn["address"],
        port=conn["port"],
        user=conn["user"],
        ssh_host_fingerprint=conn["ssh_host_fingerprint"],
        devices=tuple(data["storage"]["devices"]),
        directory=directory,
    )


def list_physical_hosts(repo_root: Path) -> tuple[PhysicalHost, ...]:
    """Вернуть физические серверы с проверенным описанием."""

    root = repo_root / "infrastructure" / "hosts"
    if not root.is_dir():
        return ()
    results: list[PhysicalHost] = []
    for directory in sorted(root.iterdir()):
        if directory.is_dir() and (directory / "host.yaml").is_file():
            results.append(load_physical_host(repo_root, directory.name))
    return tuple(results)
