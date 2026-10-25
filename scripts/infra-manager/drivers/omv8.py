"""OMV 8 physical-host driver; intentionally separate from infra-manager core."""

from __future__ import annotations
import os
import json
import shlex
import re
import sys
from pathlib import Path
from infra_manager.common import InfraManagerError, console, run
from infra_manager.physical_hosts import PhysicalHost
from infra_manager.physical_host_ssh import PhysicalHostSshSession
from infra_manager.physical_host_bootstrap import bootstrap_physical_host
from infra_manager.physical_storage import read_disk_inspection, verify_expected_disks
from infra_manager.physical_storage_plan import propose_storage, format_storage_plan, observed_disk_condition
from omv8_support.physical_nas_storage_state import apply_saved_storage
from omv8_support.physical_nas_storage_prepare import prepare_blank_disks, read_pending_recovery
from omv8_support.physical_nas_system import read_nas_system, require_omv8
from omv8_support.physical_nas_ansible import load_nas_provision, run_nas_phase
from omv8_support.physical_nas_users import require_nas_users_configured, require_nas_smb_secret
from omv8_support.physical_nas_backup_policy import load_nas_backup_policy, verify_nas_backup_volumes
from omv8_support.physical_nas_status import verify_nas_services, show_nas_status
from omv8_support.physical_nas_volumes import verify_nas_volumes


def validate_storage(storage: object) -> None:
    """OMV-specific disk and volume safety invariants."""
    roles = [entry["role"] for entry in storage.devices]
    if any(entry["by_id"] is not None for entry in storage.devices):
        raise InfraManagerError(
            f"{storage.manifest}: by_id не поддерживается этим драйвером"
        )
    for role in ("shared", "data", "backup"):
        if roles.count(role) != 1:
            raise InfraManagerError(
                f"{storage.manifest}: требуется ровно один диск роли {role}"
            )
    if {entry["id"] for entry in storage.volumes} != {
        "shared", "data", "backup",
    }:
        raise InfraManagerError(
            f"{storage.manifest}: требуются тома shared, data, backup"
        )
    expected = {"shared": "ext4", "data": "zfs_pool", "backup": "zfs_pool"}
    if {entry["id"]: entry["kind"] for entry in storage.volumes} != expected:
        raise InfraManagerError(f"{storage.manifest}: неверные файловые системы")
    for entry in storage.volumes:
        if entry["device"] != entry["id"]:
            raise InfraManagerError(f"{storage.manifest}: неверная роль тома")
    for role in ("data", "backup"):
        if storage.volume(role)["pool"] != "nas-" + role:
            raise InfraManagerError(f"{storage.manifest}: неверный ZFS pool")


def execute(repo_root: Path, operation: str, host: PhysicalHost) -> int:
    """OMV8 driver; loaded explicitly from repository driver directory."""

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
                pending = read_pending_recovery(host, session, plan)
                console.info(format_storage_plan(plan))
                if pending is not None:
                    console.warning(
                        "Обнаружен журнал ранее подтверждённой подготовки HDD. "
                        "Три файловые системы уже существуют; "
                        "будет выполнена только проверка и регистрация OMV "
                        "без форматирования"
                    )
                print(
                    "NAS_STORAGE_PLAN_JSON=" + json.dumps({
                        "fingerprint": plan.fingerprint,
                        "requires_creation": plan.requires_creation,
                        "recovery_fingerprint": (
                            pending["fingerprint"] if pending is not None else None
                        ),
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
                provision = load_nas_provision(repo_root)
                smb_access = (
                    require_nas_users_configured(repo_root)
                    if "smb" in provision["deployment"]["components"] else None
                )
                if smb_access is not None:
                    require_nas_smb_secret(smb_access)
                run_nas_phase(
                    repo_root, session, "base", provision=provision,
                    policy=load_nas_backup_policy(repo_root),
                )
                verified = verify_nas_volumes(host, system, session)
                for volume in verified:
                    console.ok(f"Том {volume.role}: {volume.mountpoint} ({volume.filesystem})")
                shared = next(volume for volume in verified if volume.role == "shared")
                shared_uuid = host.volumes["shared"]["filesystem_uuid"]
                data = next(volume for volume in verified if volume.role == "data")
                backup = next(volume for volume in verified if volume.role == "backup")
                policy = load_nas_backup_policy(repo_root)
                verify_nas_backup_volumes(
                    policy, verified,
                    expected_pools={
                        role: host.volumes[role]["pool"] for role in ("data", "backup")
                    },
                )
                run_nas_phase(
                    repo_root, session, "services", provision=provision,
                    policy=policy,
                    access=smb_access,
                    verified_volumes={volume.role: volume for volume in verified},
                    host_volumes=host.volumes,
                )
                verify_nas_services(
                    session, repo_root=repo_root, host_name=host.name,
                    disks=disks, volumes=verified,
                )
                show_nas_status(
                    repo_root, session, provision,
                    project_revision=None,
                    show_secrets=os.environ.get("INFRA_MANAGER_OPERATOR_SHOW_SECRETS") == "1",
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
                verify_nas_services(
                    connection, repo_root=repo_root, host_name=host.name,
                    disks=disks, volumes=verified,
                )
                show_nas_status(
                    repo_root, connection, load_nas_provision(repo_root),
                    project_revision=None,
                    show_secrets=os.environ.get("INFRA_MANAGER_OPERATOR_SHOW_SECRETS") == "1",
                )
        else:
            console.result("Чтение сведений о накопителях NAS завершено без изменений")
        return 0

    raise InfraManagerError(f"Неизвестная операция NAS: {operation}")
