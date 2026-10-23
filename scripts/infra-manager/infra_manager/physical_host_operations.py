"""Штатные операции над физическими серверами без создания объектов PVE."""

from __future__ import annotations

import os
import json
import shlex
import re
import sys
from pathlib import Path

import yaml

from .common import InfraManagerError, console
from .guest_catalog import find_guest_by_role
from .settings import SETTINGS
from .physical_hosts import list_physical_hosts, load_physical_host
from .physical_host_ssh import PhysicalHostSshSession
from .physical_host_bootstrap import (
    bootstrap_physical_host, resolve_saved_host, confirm_redeploy_host_key,
)
from .physical_host_ssh import discover_first_host_key
from dataclasses import replace
from .physical_storage import read_disk_inspection, verify_expected_disks
from .physical_storage_plan import propose_storage, format_storage_plan, observed_disk_condition
from .physical_nas_storage_state import apply_saved_storage
from .physical_nas_storage_prepare import prepare_blank_disks
from .physical_nas_system import read_nas_system, require_omv8
from .physical_nas_ansible import (
    configure_nas, configure_nas_apps, configure_nas_backups, configure_nas_smb,
)
from .physical_nas_status import verify_nas_services
from .physical_nas_volumes import verify_nas_volumes
from .common import run


SUPPORTED_HOST_OPERATIONS = frozenset({"hosts", "status", "inspect", "storage-plan", "storage-prepare", "deploy", "repair", "bootstrap", "ssh-trust-plan", "ssh-trust-confirm"})


def require_host_access(repo_root: Path, host_name: str) -> None:
    """Проверить явное право управляющей роли на физический сервер."""

    path = repo_root / "infrastructure" / "security" / "access.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InfraManagerError("Не удалось прочитать политику доступа проекта") from exc
    rules = document.get("rules") if isinstance(document, dict) else None
    if not isinstance(rules, list):
        raise InfraManagerError("Политика доступа не содержит корректных rules")
    identity = find_guest_by_role(repo_root, SETTINGS.infra_manager_role)
    subject = f"guest:{identity.vmid}"
    matched = [
        rule for rule in rules if isinstance(rule, dict)
        and rule.get("subject") == subject
        and rule.get("service") == "ssh"
        and rule.get("resource") == "host"
        and "connect-root" in rule.get("access", [])
        and f"host:{host_name}" in rule.get("targets", [])
    ]
    if len(matched) != 1:
        raise InfraManagerError(
            f"Доступ {subject} к host:{host_name} не разрешён однозначно в access.yaml"
        )


def list_hosts(repo_root: Path) -> int:
    """Показать перечень физических машин и готовность настроек связи."""

    hosts = list_physical_hosts(repo_root)
    if not hosts:
        console.warning("Машинные описания физических серверов отсутствуют")
        return 0
    for host in hosts:
        address = host.address or "адрес не указан"
        console.info(f"{host.name}: физический сервер, {address}")
    return 0


def run_physical_host_operation(repo_root: Path, operation: str, name: str) -> int:
    """Проверить описание цели; не выполнять необъявленных разрушительных действий."""

    if operation not in SUPPORTED_HOST_OPERATIONS:
        raise InfraManagerError(f"Недопустимая операция физического сервера: {operation}")
    if operation == "hosts":
        return list_hosts(repo_root)
    host = resolve_saved_host(load_physical_host(repo_root, name))
    require_host_access(repo_root, name)
    if operation in {"ssh-trust-plan", "ssh-trust-confirm"}:
        if not host.address or not host.ssh_host_fingerprint:
            raise InfraManagerError("Сначала требуется первоначальное SSH-доверие")
        if operation == "ssh-trust-plan":
            current = discover_first_host_key(replace(host, ssh_host_fingerprint=None))
            print("PHYSICAL_SSH_TRUST_JSON=" + json.dumps({
                "name": host.name, "address": host.address,
                "previous": host.ssh_host_fingerprint,
                "current": current.ssh_host_fingerprint,
                "changed": host.ssh_host_fingerprint != current.ssh_host_fingerprint,
            }, ensure_ascii=False, separators=(",", ":")))
            return 0
        payload = sys.stdin.readline(2048)
        if not payload or len(payload) > 2047 or not payload.endswith("\n"):
            raise InfraManagerError("Не получено подтверждение SSH-ключа")
        try:
            approved = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InfraManagerError("Некорректное подтверждение SSH-ключа") from exc
        if not isinstance(approved, dict) or set(approved) != {"fingerprint"}:
            raise InfraManagerError("Некорректная форма подтверждения SSH-ключа")
        changed = confirm_redeploy_host_key(host, approved["fingerprint"])
        console.result("SSH-ключ обновлён после подтверждения" if changed else
                       "SSH-ключ физического сервера подтверждён")
        return 0
    if host.driver != "omv8" and operation != "bootstrap":
        raise InfraManagerError(
            f"Не реализован обработчик {host.driver!r} для физического сервера "
            f"{host.name}; SSH-доверие работает независимо от типа сервера"
        )
    host = apply_saved_storage(host)
    console.info(f"Физический сервер: {host.name} (обработчик {host.driver})")
    if host.address is None:
        raise InfraManagerError(
            f"{host.directory / 'host.yaml'}: укажите сетевой адрес физического сервера; "
            "автоматический поиск соседних машин запрещён"
        )
    if host.ssh_host_fingerprint is None and operation != "bootstrap":
        raise InfraManagerError(
            f"Первое SSH-доверие {host.name} ещё не установлено. "
            f"Запустите интерактивно infra-manager deploy {host.name} на PVE"
        )

    if operation == "bootstrap":
        pve_node = os.environ.get("INFRA_PVE_NODE", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
            raise InfraManagerError("Не определён доверенный узел PVE")
        payload = sys.stdin.readline(16385)
        if not payload or len(payload) > 16384 or not payload.endswith("\n"):
            raise InfraManagerError("Некорректный закрытый ввод для первого подключения")
        bootstrap_physical_host(host, pve_node, payload)
        console.result("Начальное доверие NAS установлено, вход OpenBao подтверждён")
        return 0

    if operation == "storage-prepare":
        pve_node = os.environ.get("INFRA_PVE_NODE", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
            raise InfraManagerError("Не определён доверенный узел PVE")
        payload = sys.stdin.readline(2048)
        if not payload or len(payload) > 2047 or not payload.endswith("\n"):
            raise InfraManagerError("Не получено подтверждение плана HDD")
        try:
            approved = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InfraManagerError("Некорректное подтверждение подготовки") from exc
        if not isinstance(approved, dict) or set(approved) != {"fingerprint"}:
            raise InfraManagerError("Некорректная форма подтверждения HDD")
        with PhysicalHostSshSession(host, pve_node) as session:
            prepare_blank_disks(repo_root, host, session, approved["fingerprint"])
        return 0

    if operation in {"inspect", "status", "storage-plan", "deploy", "repair"}:
        pve_node = os.environ.get("INFRA_PVE_NODE", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
            raise InfraManagerError(
                "Не определён доверенный PVE-узел для подписи временного SSH-ключа"
            )
        console.info("Проверка SSH-ключа NAS и временного сертификата OpenBao")
        with PhysicalHostSshSession(host, pve_node) as session:
            system = read_nas_system(session)
            console.info(
                f"ОС: Debian {system.debian}, OpenMediaVault "
                f"{system.omv_version or 'не установлена'}"
            )
            result = run(
                session.command(
                    "lsblk --json --bytes --output "
                    "NAME,PATH,MODEL,SERIAL,SIZE,TYPE,FSTYPE,UUID,PTTYPE,MOUNTPOINTS"
                ),
                capture_output=True,
            )
            disks = read_disk_inspection(result.stdout)
            if operation == "storage-plan":
                require_omv8(system)
                plan = propose_storage(host.devices, disks)
                for item in plan.assignments:
                    if item.action == "preserve":
                        continue
                    # Наличие разделов в lsblk недостаточно для доказательства
                    # пустоты HDD: обязателен поиск скрытых сигнатур.
                    checked = run(
                        session.command(
                            "wipefs --no-act " + shlex.quote(item.path_for_display)
                        ),
                        capture_output=True, check=False,
                    )
                    if checked.returncode != 0 or checked.stdout.strip():
                        raise InfraManagerError(
                            "Диск " + item.serial +
                            " содержит сигнатуры или не прошёл проверку wipefs; "
                            "создание файловой системы запрещено"
                        )
                    signature = run(
                        session.command(
                            "blkid -p " + shlex.quote(item.path_for_display)
                        ),
                        capture_output=True, check=False,
                    )
                    if signature.returncode != 2 or signature.stdout.strip():
                        raise InfraManagerError(
                            "Не удалось подтвердить отсутствие сигнатур на HDD: "
                            + item.serial
                        )
                    console.ok(
                        f"Диск {item.serial}: wipefs и blkid не обнаружили сигнатур "
                        "(перед записью проверка повторится)"
                    )
                console.info(format_storage_plan(plan))
                print(
                    "NAS_STORAGE_PLAN_JSON=" + json.dumps({
                        "fingerprint": plan.fingerprint,
                        "requires_creation": plan.requires_creation,
                        "items": [
                            {"role": item.role, "serial": item.serial,
                             "action": item.action,
                             "current_state": observed_disk_condition(item)}
                            for item in plan.assignments
                        ],
                    }, ensure_ascii=False, separators=(",", ":"))
                )
                return 0
            if operation in {"deploy", "repair"}:
                require_omv8(system)
                verify_expected_disks(host.devices, disks)
                configure_nas(repo_root, session)
                verified = verify_nas_volumes(host, system, session)
                for volume in verified:
                    console.ok(f"Том {volume.role}: {volume.mountpoint} ({volume.filesystem})")
                shared = next(volume for volume in verified if volume.role == "shared")
                shared_uuid = host.volumes["shared"]["filesystem_uuid"]
                configure_nas_apps(
                    repo_root, session,
                    shared_mount=shared.mountpoint, shared_uuid=shared_uuid,
                )
                data = next(volume for volume in verified if volume.role == "data")
                backup = next(volume for volume in verified if volume.role == "backup")
                configure_nas_smb(
                    repo_root, session,
                    shared_mount=shared.mountpoint,
                    shared_uuid=shared_uuid,
                    shared_ref=host.volumes["shared"]["omv_reference"],
                    data_mountpoint=data.mountpoint,
                    data_ref=host.volumes["data"]["omv_reference"],
                )
                configure_nas_backups(
                    repo_root, session,
                    source_guid=host.volumes["data"]["pool_guid"],
                    backup_guid=host.volumes["backup"]["pool_guid"],
                    data_mountpoint=data.mountpoint,
                    backup_mountpoint=backup.mountpoint,
                )
                verify_nas_services(session, disks=disks, volumes=verified)
                console.result(
                    "NAS настроен: OMV, ZFS, SMB, Docker, qBittorrent, "
                    "расписание резервирования. Данные не форматировались"
                )
                return 0
        expected_serials = {
            item["serial"]: item["role"]
            for item in host.devices if item["serial"] is not None
        }
        for disk in disks:
            identity = disk.serial or "серийный номер отсутствует"
            role = expected_serials.get(disk.serial, "роль не подтверждена")
            condition = "системный" if disk.is_system else (
                "с существующей разметкой" if disk.contains_existing_layout
                else "без обнаруженной разметки"
            )
            console.info(
                f"{disk.path}: {disk.model} | {disk.capacity_bytes // 10**9} ГБ "
                f"| {identity} | {condition} | {role}"
            )
        if operation == "status":
            require_omv8(system)
            verify_expected_disks(host.devices, disks)
            with PhysicalHostSshSession(host, pve_node) as connection:
                verified = verify_nas_volumes(host, system, connection)
            for volume in verified:
                console.ok(f"Том {volume.role}: {volume.mountpoint} ({volume.filesystem})")
            with PhysicalHostSshSession(host, pve_node) as connection:
                verify_nas_services(connection, disks=disks, volumes=verified)
            console.result("NAS: ОС, SSH, диски, тома и службы проверены")
        else:
            console.result("Чтение сведений о накопителях NAS завершено без изменений")
        return 0

    raise InfraManagerError(f"Неизвестная операция NAS: {operation}")
