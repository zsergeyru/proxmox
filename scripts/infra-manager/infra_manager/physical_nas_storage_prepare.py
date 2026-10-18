"""Подготовка трёх новых HDD после точного подтверждения на PVE.

Все изменения выполняет проверенная программа на NAS. Управляющий гость
ещё раз проверяет план, затем независимо проверяет созданные UUID/GUID и
только после этого сохраняет результаты на постоянном томе 910.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from .common import InfraManagerError, console, run
from .physical_hosts import PhysicalHost
from .physical_host_ssh import PhysicalHostSshSession
from .physical_nas_ansible import configure_nas
from .physical_nas_storage_state import save_storage_identity
from .physical_nas_system import read_nas_system
from .physical_nas_volumes import verify_nas_volumes
from .physical_storage import read_disk_inspection
from .physical_storage_plan import StoragePlan, propose_storage

RESULT_PREFIX = "NAS_STORAGE_RESULT="


def read_storage_plan(host: PhysicalHost, session: PhysicalHostSshSession) -> StoragePlan:
    """Сверить реальный состав накопителей по SSH непосредственно перед записью."""

    result = run(
        session.command(
            "lsblk --json --bytes --output "
            "NAME,PATH,MODEL,SERIAL,SIZE,TYPE,FSTYPE,UUID,PTTYPE,MOUNTPOINTS"
        ), capture_output=True,
    )
    return propose_storage(host.devices, read_disk_inspection(result.stdout))


def _check_result(host: PhysicalHost, plan: StoragePlan,
                  response: object) -> PhysicalHost:
    """Принимать только подтверждённые номера и полное описание новых томов."""

    if not isinstance(response, dict):
        raise InfraManagerError("NAS не вернул идентификаторы подготовленных HDD")
    if response.get("fingerprint") != plan.fingerprint:
        raise InfraManagerError("NAS вернул другой контрольный отпечаток подготовки")
    serials = response.get("serials")
    volumes = response.get("volumes")
    if not isinstance(serials, dict) or not isinstance(volumes, dict):
        raise InfraManagerError("NAS вернул неполное назначение файловых систем")
    resolved_devices = []
    for assignment in plan.assignments:
        entry = next(
            (item for item in host.devices if item["role"] == assignment.role), None
        )
        if not isinstance(entry, dict):
            raise InfraManagerError("Нет ожидаемого диска в конфигурации")
        if assignment.role != "system" and serials.get(assignment.role) != assignment.serial:
            raise InfraManagerError("Серийный номер HDD изменился при подготовке")
        resolved_devices.append({**entry, "serial": assignment.serial})
    for role in ("shared", "data", "backup"):
        spec = volumes.get(role)
        configured = (host.volumes or {}).get(role)
        if not isinstance(spec, dict) or not isinstance(configured, dict):
            raise InfraManagerError(f"Нет идентификаторов тома {role}")
        for field in configured:
            if configured[field] is not None and spec.get(field) != configured[field]:
                raise InfraManagerError(f"Подготовленный том {role} не соответствует проекту")
        if not isinstance(spec.get("omv_reference"), str) or not re.fullmatch(
            r"[a-f0-9-]{36}", spec["omv_reference"]
        ):
            raise InfraManagerError("Некорректный UUID записи файловой системы OMV")
    shared = volumes["shared"]
    if not re.fullmatch(r"/srv/dev-disk-by-uuid-[a-fA-F0-9-]{16,40}", str(shared.get("mountpoint"))):
        raise InfraManagerError("Неизвестная точка подключения ext4")
    if not re.fullmatch(r"[a-fA-F0-9-]{16,40}", str(shared.get("filesystem_uuid"))):
        raise InfraManagerError("Неизвестный UUID ext4")
    for role in ("data", "backup"):
        pool = "nas-" + role
        spec = volumes[role]
        if spec.get("pool") != pool or spec.get("mountpoint") != "/" + pool:
            raise InfraManagerError("Подмена ZFS-пула или каталога подключения")
        if not re.fullmatch(r"[0-9]{1,20}", str(spec.get("pool_guid"))):
            raise InfraManagerError("Неизвестный GUID ZFS")
    return replace(host, devices=tuple(resolved_devices), volumes=volumes)


def prepare_blank_disks(repo_root: Path, host: PhysicalHost,
                        session: PhysicalHostSshSession, confirmed: str) -> None:
    """Выполнить подтверждённый план ровно один раз и сохранить результат."""

    if not re.fullmatch(r"[a-f0-9]{64}", confirmed):
        raise InfraManagerError("Подтверждение подготовки NAS имеет неверный формат")
    plan = read_storage_plan(host, session)
    if plan.fingerprint != confirmed:
        raise InfraManagerError(
            "Состав дисков или назначение изменились после просмотра плана. "
            "Подготовка отменена; получите новый план"
        )
    if [item.action for item in plan.assignments] != [
        "preserve", "create-ext4", "create-zfs", "create-zfs"
    ]:
        raise InfraManagerError(
            "Автоматическая подготовка разрешена только когда все три HDD "
            "пусты. Существующие тома не форматируются и не заменяются"
        )
    console.info("Установка необходимых средств подготовки HDD и OMV")
    configure_nas(repo_root, session)
    payload = {
        "fingerprint": confirmed,
        "items": [
            {"role": item.role, "serial": item.serial, "action": item.action}
            for item in plan.assignments if item.role != "system"
        ],
    }
    result = run(
        session.command("python3 /usr/local/sbin/nas-storage-prepare"),
        input_text=json.dumps(payload, ensure_ascii=False) + "\n",
        check=False, capture_output=True,
    )
    if result.returncode:
        raise InfraManagerError(
            "Подготовка HDD на NAS остановлена. Если началась запись, "
            "проверьте журнал /etc/nas-apps/storage-preparation.json. "
            "Повторное форматирование автоматически запрещено"
        )
    outputs = [
        line.removeprefix(RESULT_PREFIX)
        for line in result.stdout.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(outputs) != 1:
        raise InfraManagerError("NAS не подтвердил итоги подготовки дисков")
    try:
        record = json.loads(outputs[0])
    except json.JSONDecodeError as exc:
        raise InfraManagerError("Некорректный отчёт о созданных томах") from exc
    finished = _check_result(host, plan, record)
    # Независимая проверка и регистрация каждого тома уже после установки.
    system = read_nas_system(session)
    verify_nas_volumes(finished, system, session)
    save_storage_identity(finished, list(finished.devices), finished.volumes)
    console.result("HDD подготовлены, зарегистрированы в OMV и сохранены по UUID/GUID")
