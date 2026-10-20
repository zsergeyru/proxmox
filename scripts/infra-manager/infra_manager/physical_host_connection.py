"""Однократное сохранение проверенных параметров SSH физического NAS.

Адрес и отпечаток SSH-ключа не требуется записывать в публичный Git:
они подтверждаются в терминале PVE, проверяются с NAS через сеть
и сохраняются в постоянном состоянии 910. Смена адреса/ключа после
установления доверия никогда не выполняется автоматически.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from .common import InfraManagerError
from .physical_host_state import checked_record_path
from .physical_hosts import PhysicalHost
from .physical_host_ssh import _scan_verified_key
from .settings import PATHS

_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def connection_state_path(name: str) -> Path:
    """Разместить запись рядом с подтверждёнными SSH и HDD, не в Git."""

    marker = checked_record_path(name, data_root=PATHS.data_dir)
    return marker.with_name(f"{name}-connection.json")


def resolve_connection_values(
    name: str, address: str | None, fingerprint: str | None
) -> tuple[str | None, str | None]:
    """Подмешать записанное ранее подтверждение, не заменяя Git-конфликт."""

    path = connection_state_path(name)
    if path.is_symlink():
        raise InfraManagerError("Подмена записи SSH-адреса ссылкой запрещена")
    if not path.exists():
        return address, fingerprint
    if not path.is_file():
        raise InfraManagerError("Запись SSH-подключения NAS не является файлом")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfraManagerError("Повреждена запись SSH-подключения NAS") from exc
    if not isinstance(record, dict) or record.get("name") != name:
        raise InfraManagerError("Сохранено подтверждение другого физического сервера")
    saved_address, saved_fingerprint = record.get("address"), record.get("fingerprint")
    try:
        ipaddress.ip_address(saved_address)
    except (ValueError, TypeError) as exc:
        raise InfraManagerError("Некорректный сохранённый IP-адрес NAS") from exc
    if not isinstance(saved_fingerprint, str) or not _FINGERPRINT.fullmatch(saved_fingerprint):
        raise InfraManagerError("Некорректный сохранённый SSH-отпечаток NAS")
    if (address is not None and address != saved_address) or (
        fingerprint is not None and fingerprint != saved_fingerprint
    ):
        raise InfraManagerError(
            "SSH-адрес или ключ в Git не совпадает с уже подтверждённым NAS; "
            "необходимо отдельно проверить смену оборудования"
        )
    return saved_address, saved_fingerprint


def apply_saved_connection(host: PhysicalHost) -> PhysicalHost:
    """Применить сохранённый адрес без повторного выбора машины."""

    address, fingerprint = resolve_connection_values(
        host.name, host.address, host.ssh_host_fingerprint
    )
    return replace(host, address=address, ssh_host_fingerprint=fingerprint)


def save_verified_connection(
    host: PhysicalHost, address: str, fingerprint: str
) -> None:
    """Сверить SSH-ключ с введённым оператором и записать подтверждение один раз."""

    try:
        ipaddress.ip_address(address)
    except ValueError as exc:
        raise InfraManagerError("Введите корректный IPv4- или IPv6-адрес NAS") from exc
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise InfraManagerError("SSH-отпечаток должен иметь вид SHA256:...")
    if (host.address is not None and host.address != address) or (
        host.ssh_host_fingerprint is not None
        and host.ssh_host_fingerprint != fingerprint
    ):
        raise InfraManagerError("Адрес или отпечаток противоречит host.yaml")
    path = connection_state_path(host.name)
    if path.is_symlink() or path.exists():
        raise InfraManagerError(
            "SSH-подключение уже подтверждено; автоматическая замена запрещена"
        )
    if checked_record_path(host.name, data_root=PATHS.data_dir).exists() or (
        checked_record_path(host.name, storage=True, data_root=PATHS.data_dir).exists()
    ):
        raise InfraManagerError(
            "Есть прежнее SSH-доверие или HDD NAS. Нельзя создать новую "
            "идентичность NAS без проверки сохранённого состояния"
        )
    candidate_host = replace(
        host, address=address, ssh_host_fingerprint=fingerprint
    )
    # Нельзя подтверждать ключ только на основании ssh-keyscan: оператор
    # обязан ввести отпечаток, отдельно сверенный на консоли самого NAS.
    with tempfile.TemporaryDirectory(prefix="nas-connection-") as directory:
        _scan_verified_key(candidate_host, Path(directory))
    payload = json.dumps(
        {"name": host.name, "address": address, "fingerprint": fingerprint},
        ensure_ascii=False,
    )
    # O_EXCL не даст заменить уже сохранённую запись даже при параллельном deploy.
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError as exc:
        raise InfraManagerError("SSH-подключение NAS уже записано") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())
