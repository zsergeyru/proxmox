"""Подтверждение томов NAS перед любыми действиями с пользовательскими файлами."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass

from .common import InfraManagerError, run
from .physical_host_ssh import PhysicalHostSshSession
from .physical_hosts import PhysicalHost
from .physical_nas_system import NasSystem


@dataclass(frozen=True)
class VerifiedVolume:
    """Том, одновременно проверенный в ядре и в настройках OMV."""

    role: str
    mountpoint: str
    source: str
    filesystem: str


def _configured_path(raw: object) -> str:
    """Не принимать относительные пути, системный корень и подмену каталога."""

    if not isinstance(raw, str) or not raw.startswith("/") or raw == "/":
        raise InfraManagerError("Путь тома NAS не задан или указывает на системный корень")
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", raw)
        or ".." in raw.split("/")
        or posixpath.normpath(raw) != raw
        or any(raw == prefix or raw.startswith(prefix + "/") for prefix in ("/etc", "/boot", "/var", "/usr", "/root"))
    ):
        raise InfraManagerError("Небезопасный путь подключения тома NAS")
    return raw


def _parse_findmnt(raw: str, mountpoint: str) -> dict[str, str]:
    """Требовать отдельный реально подключённый том, а не родительский SSD."""

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfraManagerError("findmnt вернул некорректный JSON") from exc
    items = parsed.get("filesystems") if isinstance(parsed, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise InfraManagerError(f"Не удалось однозначно проверить подключение {mountpoint}")
    item = items[0]
    if not isinstance(item, dict) or item.get("target") != mountpoint:
        raise InfraManagerError(
            f"Том {mountpoint} не подключён: запись на SSD запрещена"
        )
    fields = ("source", "fstype")
    if any(not isinstance(item.get(field), str) for field in fields):
        raise InfraManagerError(f"Отсутствуют сведения о томе {mountpoint}")
    return item


def _registered_in_omv(
    omv_mounts: tuple[dict[str, object], ...], reference: str, mountpoint: str,
) -> None:
    """Проверить, что именно этот том зарегистрирован средствами OMV."""

    entries = [
        entry for entry in omv_mounts
        if entry.get("uuid") == reference and entry.get("dir") == mountpoint
    ]
    if len(entries) != 1:
        raise InfraManagerError(
            f"Том {mountpoint} не соответствует записи OpenMediaVault {reference}"
        )


def verify_nas_volumes(
    host: PhysicalHost, system: NasSystem, session: PhysicalHostSshSession,
) -> tuple[VerifiedVolume, ...]:
    """Проверить все три роли до создания каталогов и запуска служб."""

    volumes = host.volumes
    if not isinstance(volumes, dict):
        raise InfraManagerError("Не заданы постоянные идентификаторы томов NAS")
    verified: list[VerifiedVolume] = []
    used_paths: set[str] = set()
    for role in ("shared", "data", "backup"):
        spec = volumes.get(role)
        if not isinstance(spec, dict):
            raise InfraManagerError(f"Отсутствует конфигурация тома {role}")
        mountpoint = _configured_path(spec.get("mountpoint"))
        if mountpoint in used_paths:
            raise InfraManagerError("Несколько томов NAS назначены одному каталогу")
        used_paths.add(mountpoint)
        omv_reference = spec.get("omv_reference")
        if not isinstance(omv_reference, str) or not omv_reference:
            raise InfraManagerError(f"Том {role}: не задан подтверждённый UUID записи OMV")
        _registered_in_omv(system.omv_mounts, omv_reference, mountpoint)
        response = run(
            session.command(
                "findmnt --json --target " + shlex.quote(mountpoint) +
                " --output TARGET,SOURCE,FSTYPE,UUID"
            ), check=False, capture_output=True,
        )
        if response.returncode:
            raise InfraManagerError(f"Не подключён ожидаемый том {role}: {mountpoint}")
        actual = _parse_findmnt(response.stdout, mountpoint)
        fs = actual["fstype"]
        source = actual["source"]
        if role == "shared":
            uid = spec.get("filesystem_uuid")
            if not isinstance(uid, str) or not uid:
                raise InfraManagerError("Не подтверждён UUID файловой системы тома shared")
            if fs != "ext4" or actual.get("uuid") != uid:
                raise InfraManagerError(
                    "Том shared не соответствует UUID/ext4 в машинном описании"
                )
        else:
            pool = spec.get("pool")
            guid = spec.get("pool_guid")
            if (
                not isinstance(pool, str) or not re.fullmatch(r"nas-[a-z]+", pool)
                or not isinstance(guid, str) or not re.fullmatch(r"[0-9]{1,20}", guid)
            ):
                raise InfraManagerError(f"Не подтверждены имя и GUID пула {role}")
            if fs != "zfs" or source.split("/", 1)[0] != pool:
                raise InfraManagerError(f"На месте {role} отсутствует пул ZFS {pool}")
            actual_guid = run(
                session.command("zpool get -H -o value guid " + pool),
                check=False, capture_output=True,
            )
            if actual_guid.returncode or actual_guid.stdout.strip() != guid:
                raise InfraManagerError(f"GUID пула ZFS {pool} изменился")
        verified.append(VerifiedVolume(role, mountpoint, source, fs))
    if len(verified) != 3:
        raise InfraManagerError("Проверены не все тома NAS")
    return tuple(verified)
