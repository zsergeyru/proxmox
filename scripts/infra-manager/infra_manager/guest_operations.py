"""Единые операции над гостями для Semaphore."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .access import GuestRecord, load_access_policy
from .common import (
    InfraManagerError,
    cancel_runtime_activation,
    console,
    require_runtime_activation_idle,
    reserve_runtime_activation,
)
from .guest_catalog import GuestIdentity, guest_identity
from .guest_deploy import run_deploy_guest
from .pve import PveClient
from .settings import SETTINGS

GUEST_OPERATIONS = ("deploy", "status", "repair", "test", "sync")


def _guest_record(repo_root: Path, vmid: int) -> GuestRecord:
    policy = load_access_policy(repo_root)
    record = policy.guests.get(vmid)
    if record is None or not record.provisioned_linux:
        raise InfraManagerError(
            f"Гость {vmid} не является поддерживаемым Linux-гостем проекта"
        )
    return record


def _expected_resource_type(record: GuestRecord) -> str:
    return "qemu" if record.kind == "vm" else "lxc"


def _checked_resource(
    client: PveClient,
    record: GuestRecord,
) -> dict[str, Any]:
    resource = client.find_vm(record.vmid)
    if resource is None:
        raise InfraManagerError(f"Гость {record.vmid} отсутствует в PVE")

    actual_type = str(resource.get("type") or "")
    expected_type = _expected_resource_type(record)
    if actual_type != expected_type:
        raise InfraManagerError(
            f"VMID {record.vmid} имеет тип {actual_type!r}, "
            f"ожидается {expected_type!r}"
        )

    actual_name = str(resource.get("name") or "")
    if actual_name != record.name:
        raise InfraManagerError(
            f"VMID {record.vmid} имеет имя {actual_name!r}, "
            f"ожидается {record.name!r}"
        )

    actual_node = str(resource.get("node") or "")
    if actual_node and actual_node != record.node:
        raise InfraManagerError(
            f"Гость {record.vmid} находится на узле {actual_node!r}, "
            f"ожидается {record.node!r}"
        )
    return resource


def _infra_manager_status(_: Path, __: GuestIdentity) -> None:
    from .status import check_status

    check_status(full=True, quiet=True)


STATUS_HANDLERS: dict[str, Callable[[Path, GuestIdentity], None]] = {
    SETTINGS.infra_manager_role: _infra_manager_status,
}


def status_guest(repo_root: Path, vmid: int) -> int:
    """Проверить текущее состояние гостя без изменений."""

    identity = guest_identity(repo_root, vmid)
    record = _guest_record(repo_root, vmid)
    client = PveClient()
    resource = _checked_resource(client, record)

    status = str(resource.get("status") or "")
    if status != "running":
        raise InfraManagerError(
            f"Гость {vmid} {identity.name} не запущен: {status or 'unknown'}"
        )

    handler = STATUS_HANDLERS.get(identity.role or "")
    if handler is not None:
        handler(repo_root, identity)

    console.ok(f"Гость {vmid} {identity.name}: состояние штатное")
    return 0


def repair_guest(repo_root: Path, vmid: int) -> int:
    """Выполнить только безопасное базовое исправление гостя."""

    identity = guest_identity(repo_root, vmid)
    record = _guest_record(repo_root, vmid)
    client = PveClient()
    resource = _checked_resource(client, record)
    status = str(resource.get("status") or "")

    if status != "running":
        endpoint_kind = _expected_resource_type(record)
        console.info(f"Запуск гостя {vmid} {identity.name}")
        client.run_task(
            "POST",
            f"/nodes/{record.node}/{endpoint_kind}/{vmid}/status/start",
            node=record.node,
        )
    else:
        console.info(
            f"Гость {vmid} {identity.name} уже запущен; "
            "разрушительные действия не требуются"
        )

    return status_guest(repo_root, vmid)


def test_guest(repo_root: Path, vmid: int) -> int:
    """Выполнить расширенную безопасную проверку гостя."""

    status_guest(repo_root, vmid)
    record = _guest_record(repo_root, vmid)
    client = PveClient()

    actual = client.guest_management_ipv4(
        node=record.node,
        vmid=record.vmid,
        kind=record.kind,
        subnet=record.subnet,
    )
    if actual is None:
        raise InfraManagerError(
            f"Гость {vmid}: PVE не видит административный IPv4"
        )
    if record.address is not None and str(actual) != record.address:
        raise InfraManagerError(
            f"Гость {vmid}: административный IPv4 {actual} "
            f"не совпадает с ожидаемым {record.address}"
        )

    console.ok(
        f"Гость {vmid}: расширенная проверка PVE и сети пройдена"
    )
    return 0


def _sync_infra_manager(repo_root: Path, identity: GuestIdentity) -> int:
    from .semaphore import configure_project

    console.info(
        f"Синхронизация управляющих данных {identity.name} "
        "из текущей рабочей копии Git"
    )
    return configure_project(
        branch=SETTINGS.project_branch(),
        repo_root=repo_root,
    )


SYNC_HANDLERS: dict[str, Callable[[Path, GuestIdentity], int]] = {
    SETTINGS.infra_manager_role: _sync_infra_manager,
}


def sync_guest(repo_root: Path, vmid: int) -> int:
    """Синхронизировать данные роли без полного Deploy."""

    identity = guest_identity(repo_root, vmid)
    _guest_record(repo_root, vmid)
    handler = SYNC_HANDLERS.get(identity.role or "")
    if handler is None:
        role = identity.role or "(не задана)"
        raise InfraManagerError(
            f"Sync для роли {role!r} пока не реализован; "
            "используйте Deploy Guest или добавьте обработчик роли"
        )
    result = handler(repo_root, identity)
    if result == 0:
        console.ok(f"Гость {vmid} {identity.name}: синхронизация завершена")
    return result


def deploy_guest(
    repo_root: Path,
    vmid: int,
    *,
    bootstrap_scope: bool = False,
    phase: str = "all",
) -> int:
    """Выполнить общий Deploy с безопасным самообновлением infra-manager."""

    require_runtime_activation_idle()
    identity = guest_identity(repo_root, vmid)
    self_update = (
        identity.role == SETTINGS.infra_manager_role
        and not bootstrap_scope
        and phase == "all"
    )
    activation_reserved = False

    try:
        if self_update:
            reserve_runtime_activation()
            activation_reserved = True

        result = run_deploy_guest(
            repo_root,
            vmid,
            bootstrap_scope=bootstrap_scope,
            phase=phase,
        )
        if result != 0 and activation_reserved:
            cancel_runtime_activation()
        return result
    except BaseException:
        if activation_reserved:
            cancel_runtime_activation()
        raise


def run_guest_operation(
    repo_root: Path,
    vmid: int,
    operation: str,
) -> int:
    """Выполнить одну стандартную операцию выбранного гостя."""

    if operation not in GUEST_OPERATIONS:
        raise InfraManagerError(f"Неизвестная операция гостя: {operation}")

    if operation == "deploy":
        return deploy_guest(repo_root, vmid)

    require_runtime_activation_idle()
    if operation == "status":
        return status_guest(repo_root, vmid)
    if operation == "repair":
        return repair_guest(repo_root, vmid)
    if operation == "test":
        return test_guest(repo_root, vmid)
    if operation == "sync":
        return sync_guest(repo_root, vmid)
    raise InfraManagerError(f"Неизвестная операция гостя: {operation}")
