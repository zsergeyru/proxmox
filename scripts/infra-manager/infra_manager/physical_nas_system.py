"""Проверка вручную установленного NAS без изменения его состояния.

Debian и OpenMediaVault устанавливает оператор. Управляющий гость проверяет
их версии и регистрацию томов именно в OMV.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .common import InfraManagerError, run
from .physical_host_ssh import PhysicalHostSshSession


@dataclass(frozen=True)
class NasSystem:
    """Проверенные сведения о состоянии ОС и OMV."""

    debian: str
    omv_version: str | None
    architecture: str
    omv_mounts: tuple[dict[str, object], ...]


def parse_os_release(raw: str) -> str:
    """Подтвердить Debian 13 без выполнения текста /etc/os-release."""

    values: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    if values.get("ID") != "debian" or values.get("VERSION_ID") != "13":
        raise InfraManagerError(
            "NAS должен использовать Debian 13; установка ОС управляющим гостем запрещена"
        )
    return values["VERSION_ID"]


def parse_omv_mounts(raw: str) -> tuple[dict[str, object], ...]:
    """Прочитать регистрацию томов OMV и отвергнуть неоднозначные записи."""

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfraManagerError("OMV вернул некорректный перечень файловых систем") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise InfraManagerError("OMV вернул неизвестный формат списка томов")
    entries: list[dict[str, object]] = []
    ids: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise InfraManagerError("OMV содержит некорректное описание тома")
        uid = entry.get("uuid")
        directory = entry.get("dir")
        if not isinstance(uid, str) or not uid or not isinstance(directory, str):
            raise InfraManagerError("OMV содержит том без идентификатора или пути")
        if uid in ids:
            raise InfraManagerError(f"Повторяющийся UUID записи OMV: {uid}")
        ids.add(uid)
        entries.append(entry)
    return tuple(entries)


def read_nas_system(session: PhysicalHostSshSession) -> NasSystem:
    """Проверить ОС и OMV 8 по доверенному SSH-соединению."""

    os_result = run(session.command("cat /etc/os-release"),
                    check=False, capture_output=True)
    if os_result.returncode:
        raise InfraManagerError("Невозможно получить версию Debian на NAS")
    debian = parse_os_release(os_result.stdout)
    arch = run(session.command("dpkg --print-architecture"),
               check=False, capture_output=True)
    if arch.returncode or arch.stdout.strip() != "amd64":
        raise InfraManagerError("NAS должен использовать архитектуру amd64")
    result = run(
        session.command("dpkg-query -W -f='${Version}' openmediavault"),
        check=False, capture_output=True
    )
    version = result.stdout.strip() if result.returncode == 0 else None
    if version is not None and not re.fullmatch(r"[0-9A-Za-z.+:~-]+", version):
        raise InfraManagerError("Не удалось подтвердить версию OpenMediaVault")
    mounts: tuple[dict[str, object], ...] = ()
    if version is not None:
        response = run(
            session.command("omv-confdbadm read conf.system.filesystem.mountpoint"),
            check=False, capture_output=True
        )
        if response.returncode:
            raise InfraManagerError(
                "OpenMediaVault установлена, но база её томов недоступна"
            )
        mounts = parse_omv_mounts(response.stdout)
    return NasSystem(
        debian=debian, omv_version=version,
        architecture=arch.stdout.strip(), omv_mounts=mounts
    )


def require_omv8(system: NasSystem) -> None:
    """Остановить развёртывание, если OMV 8 не установлена вручную."""

    version = system.omv_version or ""
    if system.debian != "13" or not re.match(r"^(?:[0-9]+:)?8\.", version):
        raise InfraManagerError(
            "Ожидается Debian 13 с OpenMediaVault 8. "
            "Сначала установите их вручную, затем повторите deploy nas"
        )
