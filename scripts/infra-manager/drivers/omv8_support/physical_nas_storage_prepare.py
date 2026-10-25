"""Подготовка новых HDD с сохранением существующего ext4 после подтверждения.

Все изменения выполняет проверенная программа на NAS. Управляющий гость
ещё раз проверяет план, затем независимо проверяет созданные UUID/GUID и
только после этого сохраняет результаты в постоянном хранилище Infra Manager.
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import replace
from pathlib import Path

from infra_manager.common import InfraManagerError, console, run
from infra_manager.physical_hosts import PhysicalHost
from infra_manager.physical_host_ssh import PhysicalHostSshSession
from .physical_nas_ansible import load_nas_provision, run_nas_phase
from .physical_nas_backup_policy import load_nas_backup_policy
from .physical_nas_storage_state import save_storage_identity
from .physical_nas_system import read_nas_system
from .physical_nas_volumes import verify_nas_volumes
from infra_manager.physical_storage import read_disk_inspection
from infra_manager.physical_storage_plan import StoragePlan, propose_storage

RESULT_PREFIX = "NAS_STORAGE_RESULT="

def _safe_storage_failure(stderr: str) -> str:
    """Вывести только известные операции и коды, без содержимого stdout."""

    allowed = {
        "omv-confdbadm", "omv-salt", "mount", "findmnt", "zpool",
        "sgdisk", "mkfs.ext4", "blkid", "udevadm", "lsblk", "wipefs",
    }
    for line in stderr.splitlines():
        match = re.fullmatch(
            r"ОШИБКА: Не выполнилась команда ([a-z0-9.-]{1,32}) "
            r"\(код ([0-9]{1,3})\)", line.strip()
        )
        if match and match.group(1) in allowed:
            return (
                f"Команда {match.group(1)} завершилась с кодом "
                f"{match.group(2)}"
            )
    if "Журнал не соответствует" in stderr or "Подтверждение не совпадает" in stderr:
        return "Исходный журнал HDD не соответствует проверенным дискам"
    if "Пул " in stderr and " не принадлежит " in stderr:
        return "Один из существующих ZFS-пулов использует другой диск"
    return "Удалённый исполнитель завершился с ошибкой (подробности скрыты для защиты данных)"



def read_pending_recovery(host: PhysicalHost, session: PhysicalHostSshSession,
                          plan: StoragePlan) -> dict[str, object] | None:
    """Проверить журнал старого подтверждения без доверия к текущим /dev/sdX."""

    # После успешной независимой проверки 910 сохраняет UUID/GUID в
    # persistent-state. Сам аварийный журнал NAS не удаляется, но больше
    # не служит причиной для повторной операции при штатном deploy.
    volumes = host.volumes or {}
    if all(
        isinstance(volumes.get(role), dict)
        and isinstance(volumes[role].get(field), str)
        and volumes[role][field]
        for role, field in (
            ("shared", "filesystem_uuid"),
            ("data", "pool_guid"),
            ("backup", "pool_guid"),
        )
    ):
        return None

    command = (
        "if test -L /etc/nas-apps/storage-preparation.json; then exit 70; "
        "elif test -f /etc/nas-apps/storage-preparation.json; then "
        "cat /etc/nas-apps/storage-preparation.json; fi"
    )
    result = run(session.command(command), capture_output=True, check=False)
    if result.returncode:
        raise InfraManagerError("Невозможно безопасно прочитать журнал подготовки NAS")
    if not result.stdout.strip():
        return None
    try:
        record = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise InfraManagerError("Журнал подготовки HDD повреждён") from exc
    if (
        not isinstance(record, dict)
        or set(record) != {"fingerprint", "items", "state"}
        or record.get("state") != "started"
        or not isinstance(record.get("fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["fingerprint"])
    ):
        raise InfraManagerError("Незавершённый журнал NAS имеет неизвестный формат")
    original = record.get("items")
    if not isinstance(original, list) or len(original) != 3:
        raise InfraManagerError("Журнал NAS содержит неполный список HDD")
    expected_actions = {
        "shared": "create-ext4",
        "data": "create-zfs",
        "backup": "create-zfs",
    }
    current = {item.role: item for item in plan.assignments}
    if set(current) != {"system", "shared", "data", "backup"}:
        raise InfraManagerError("Невозможно сверить текущий состав дисков NAS")
    previous = {item.get("role"): item for item in original
                if isinstance(item, dict)}
    if set(previous) != set(expected_actions) or len(previous) != 3:
        raise InfraManagerError("Журнал NAS содержит неоднозначные назначения HDD")
    for role, original_item in previous.items():
        if (
            not isinstance(original_item, dict)
            or set(original_item) != {"role", "serial", "action"}
            or original_item["action"] != expected_actions[role]
            or original_item["serial"] != current[role].serial
            or current[role].action != "preserve"
        ):
            raise InfraManagerError(
                "Диски не совпадают с подтверждённой попыткой подготовки. "
                "Никакие операции над HDD не выполнялись"
            )
    if current["system"].action != "preserve":
        raise InfraManagerError("Системный SSD NAS не подтверждён")
    # План образуется из старых действий, фактических моделей, размеров и
    # серийных номеров, а не из уже изменившейся разметки после форматирования.
    former = []
    for role in ("system", "shared", "data", "backup"):
        item = current[role]
        former.append({
            "role": role,
            "serial": item.serial,
            "model": item.model,
            "size": item.size,
            "action": "preserve" if role == "system" else expected_actions[role],
        })
    actual_fingerprint = hashlib.sha256(
        json.dumps(former, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if actual_fingerprint != record["fingerprint"]:
        raise InfraManagerError(
            "Журнал HDD не соответствует исходному подтверждённому плану"
        )
    return record



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
                  response: object, *,
                  expected_fingerprint: str | None = None) -> PhysicalHost:
    """Проверить результат по исходному подтверждению, а не новому плану preserve."""

    if not isinstance(response, dict):
        raise InfraManagerError("NAS не вернул идентификаторы подготовленных HDD")
    fingerprint = plan.fingerprint if expected_fingerprint is None else expected_fingerprint
    if response.get("fingerprint") != fingerprint:
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
    pending = read_pending_recovery(host, session, plan)
    if pending is not None:
        if confirmed != pending["fingerprint"]:
            raise InfraManagerError("Отпечаток не совпадает с исходным журналом HDD")
        console.info(
            "Продолжение незавершённой подготовки NAS: только регистрация "
            "существующих томов OMV, БЕЗ форматирования"
        )
    else:
        if plan.fingerprint != confirmed:
            raise InfraManagerError(
                "Состав дисков или назначение изменились после просмотра плана. "
                "Подготовка отменена; получите новый план"
            )
        actions = [item.action for item in plan.assignments]
        if actions not in (
            ["preserve", "create-ext4", "create-zfs", "create-zfs"],
            ["preserve", "preserve", "create-zfs", "create-zfs"],
        ):
            raise InfraManagerError(
                "Для автоматической подготовки требуются два пустых ZFS HDD. "
                "Диск shared может быть пустым либо существующим ext4; "
                "его сохранность обязательно подтвердит NAS до первой записи"
            )
    console.info("Установка необходимых средств подготовки HDD и OMV")
    run_nas_phase(
        repo_root, session, 'base',
        provision=load_nas_provision(repo_root),
        policy=load_nas_backup_policy(repo_root),
    )
    payload = {
        "fingerprint": confirmed,
        "items": (
            pending["items"] if pending is not None
            else [
                {"role": item.role, "serial": item.serial, "action": item.action}
                for item in plan.assignments if item.role != "system"
            ]
        ),
    }
    result = run(
        session.command(
            "python3 /usr/local/sbin/nas-storage-prepare"
            + (" recover" if pending is not None else "")
        ),
        input_text=json.dumps(payload, ensure_ascii=False) + "\n",
        check=False, capture_output=True,
    )
    if result.returncode:
        raise InfraManagerError(
            "Подготовка HDD на NAS остановлена: "
            + _safe_storage_failure(result.stderr or "")
            + ". Если началась запись, проверьте журнал "
            "/etc/nas-apps/storage-preparation.json. "
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
    # В режиме recover NAS возвращает исходный отпечаток из сохранённого
    # журнала создания, а текущий plan.fingerprint уже отражает preserve.
    finished = _check_result(host, plan, record, expected_fingerprint=confirmed)
    # Независимая проверка и регистрация каждого тома уже после установки.
    system = read_nas_system(session)
    verify_nas_volumes(finished, system, session)
    save_storage_identity(finished, list(finished.devices), finished.volumes)
    console.result(
        ("Ранее созданные HDD проверены и зарегистрированы в OMV "
         "без повторного форматирования" if pending is not None else
         "HDD подготовлены, зарегистрированы в OMV и сохранены по UUID/GUID")
    )
