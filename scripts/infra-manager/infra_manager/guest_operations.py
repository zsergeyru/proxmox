"""Единые штатные операции над гостями проекта."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml

from .common import (
    InfraManagerError,
    cancel_runtime_activation,
    console,
    require_runtime_activation_idle,
    reserve_runtime_activation,
)
from .guest_catalog import GuestIdentity, deployable_guests, guest_identity
from .guest_deploy import (
    project_branch_for_checkout,
    project_revision,
    run_deploy_guest,
)
from .guest_status import show_guest_status, verify_guest_status
from .operation_lock import guest_operation_lock, project_checkout_lock
from .pve import PveClient
from .pve_host import (
    install_openbao_host_support,
    repair_openbao_on_host,
)
from .settings import SETTINGS

GUEST_OPERATIONS: Final[tuple[str, ...]] = (
    "deploy",
    "status",
    "repair",
    "test",
    "sync",
)

GUEST_OPERATION_TEMPLATES: Final[dict[str, str]] = {
    "deploy": "Deploy Guest",
    "status": "Status Guest",
    "repair": "Repair Guest",
    "test": "Test Guest",
    "sync": "Sync Guest",
}


def extract_survey_vmid(argv: list[str]) -> tuple[list[str], int | None]:
    """Извлечь GUEST_VMID, который Semaphore передаёт как survey-переменную."""

    remaining: list[str] = []
    values: list[str] = []
    for item in argv:
        if item.startswith("GUEST_VMID="):
            values.append(item.split("=", 1)[1])
        else:
            remaining.append(item)
    if len(values) > 1:
        raise InfraManagerError("GUEST_VMID передан более одного раза")
    if not values:
        return remaining, None
    if not values[0].isdigit() or int(values[0]) <= 0:
        raise InfraManagerError("GUEST_VMID должен быть положительным VMID")
    return remaining, int(values[0])


def operation_guests(
    repo_root: Path,
    operation: str,
) -> tuple[GuestIdentity, ...]:
    """Вернуть гостей, для которых операция доступна в Semaphore."""

    if operation not in GUEST_OPERATIONS:
        raise InfraManagerError(f"Неизвестная операция гостя: {operation}")

    return deployable_guests(repo_root)


def _expected_resource_type(repo_root: Path, identity: GuestIdentity) -> str:
    """Определить ожидаемый тип ресурса PVE по профилю гостя."""

    profile = identity.source.get("profile")
    if not isinstance(profile, str) or not profile:
        raise InfraManagerError(f"{identity.manifest}: не задан profile")

    defaults_path = repo_root / "infrastructure" / "guests" / "defaults.yaml"
    try:
        defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(
            f"Не удалось прочитать {defaults_path}: {exc}"
        ) from exc
    profiles = defaults.get("profiles") if isinstance(defaults, dict) else None
    profile_data = profiles.get(profile) if isinstance(profiles, dict) else None
    kind = profile_data.get("type") if isinstance(profile_data, dict) else None
    if kind == "vm":
        return "qemu"
    if kind == "lxc":
        return "lxc"
    raise InfraManagerError(
        f"{defaults_path}: профиль {profile!r} имеет неизвестный type"
    )


def _guest_resource(
    client: PveClient,
    repo_root: Path,
    identity: GuestIdentity,
) -> dict[str, object]:
    """Найти объект PVE и подтвердить его идентичность."""

    resource = client.find_vm(identity.vmid)
    if resource is None:
        raise InfraManagerError(
            f"Гость {identity.vmid} {identity.name} отсутствует в PVE"
        )

    actual_name = resource.get("name")
    actual_type = resource.get("type")
    expected_type = _expected_resource_type(repo_root, identity)
    if actual_name != identity.name or actual_type != expected_type:
        raise InfraManagerError(
            f"VMID {identity.vmid} занят объектом "
            f"{actual_name!r} типа {actual_type!r}; ожидался "
            f"{identity.name!r} типа {expected_type!r}"
        )

    node = resource.get("node")
    if not isinstance(node, str) or not node:
        raise InfraManagerError(
            f"PVE не вернул узел для гостя {identity.vmid}"
        )
    return resource


def _require_running(resource: dict[str, object], identity: GuestIdentity) -> None:
    if resource.get("status") != "running":
        raise InfraManagerError(
            f"Гость {identity.vmid} {identity.name} не запущен"
        )


def _generic_status(
    client: PveClient,
    repo_root: Path,
    identity: GuestIdentity,
    *,
    announce: bool = True,
) -> dict[str, object]:
    resource = _guest_resource(client, repo_root, identity)
    _require_running(resource, identity)
    if announce:
        console.ok(
            f"Гость {identity.vmid} {identity.name}: объект PVE запущен"
        )
    return resource


def _infra_manager_repair(
    node: str,
    repo_root: Path,
    identity: GuestIdentity,
) -> None:
    del identity
    from .semaphore import sync_project_from_task

    console.info("Обновление PVE-only OpenBao helper")
    install_openbao_host_support(node, repo_root)
    console.info("Безопасное восстановление OpenBao")
    repair_openbao_on_host(node)
    console.info("Синхронизация проекта Semaphore")
    sync_project_from_task(
        branch=project_branch_for_checkout(repo_root),
        repo_root=repo_root,
    )
    console.ok(
        "OpenBao восстановлен, проект Semaphore синхронизирован; "
        "полная готовность проверяется отдельным Status Guest"
    )


def _infra_manager_sync(
    repo_root: Path,
    identity: GuestIdentity,
) -> None:
    del identity
    from .semaphore import sync_project_from_task

    console.info(
        "Синхронизация Semaphore из Git-версии текущего задания"
    )
    sync_project_from_task(
        branch=project_branch_for_checkout(repo_root),
        repo_root=repo_root,
    )
    console.ok("Конфигурация Semaphore синхронизирована")


def _run_deploy(
    repo_root: Path,
    identity: GuestIdentity,
    *,
    show_secrets: bool = False,
) -> int:
    """Выполнить Deploy Guest и показать единый итоговый статус."""

    activation_reserved = False
    try:
        self_update = identity.role == SETTINGS.infra_manager_role
        if self_update:
            reserve_runtime_activation()
            activation_reserved = True

        result = run_deploy_guest(repo_root, identity.vmid)
        if result != 0:
            if activation_reserved:
                cancel_runtime_activation()
            return result

        client = PveClient.from_opentofu_env()
        resource = _generic_status(
            client,
            repo_root,
            identity,
            announce=False,
        )
        show_guest_status(
            repo_root,
            identity,
            resource,
            full=True,
            show_secrets=show_secrets,
            project_branch=project_branch_for_checkout(repo_root),
            project_revision=project_revision(repo_root),
        )
        return 0
    except BaseException:
        if activation_reserved:
            cancel_runtime_activation()
        raise


def _run_status(
    client: PveClient,
    repo_root: Path,
    identity: GuestIdentity,
    *,
    show_secrets: bool = False,
) -> int:
    resource = _generic_status(
        client,
        repo_root,
        identity,
        announce=False,
    )
    show_guest_status(
        repo_root,
        identity,
        resource,
        full=True,
        show_secrets=show_secrets,
        project_branch=project_branch_for_checkout(repo_root),
        project_revision=project_revision(repo_root),
    )
    return 0


def list_local_guests(repo_root: Path) -> int:
    """Показать поддерживаемых гостей и состояние объектов PVE."""

    with project_checkout_lock(exclusive=False):
        client = PveClient()
        print(f"{'VMID':<7}{'Имя':<24}{'Роль':<20}Состояние")
        for identity in deployable_guests(repo_root):
            resource = client.find_vm(identity.vmid)
            if resource is None:
                state = "отсутствует"
            else:
                state = str(resource.get("status") or "неизвестно")
            print(
                f"{identity.vmid:<7}"
                f"{identity.name:<24}"
                f"{(identity.role or '-'):<20}"
                f"{state}"
            )
    return 0


def run_local_guest_status(
    repo_root: Path,
    vmid: int,
    *,
    show_secrets: bool = False,
) -> int:
    """Показать статус гостя из управляющего контура с PVE credential."""

    require_runtime_activation_idle()
    with project_checkout_lock(exclusive=False):
        identity = guest_identity(repo_root, vmid)
        return _run_status(
            PveClient(),
            repo_root,
            identity,
            show_secrets=show_secrets,
        )


def _run_repair(
    client: PveClient,
    repo_root: Path,
    identity: GuestIdentity,
) -> int:
    resource = _guest_resource(client, repo_root, identity)
    node = str(resource["node"])

    if resource.get("status") != "running":
        resource_type = str(resource["type"])
        console.info(
            f"Запуск гостя {identity.vmid} {identity.name}"
        )
        client.run_task(
            "POST",
            f"/nodes/{node}/{resource_type}/{identity.vmid}/status/start",
            node=node,
        )
        resource = _guest_resource(client, repo_root, identity)
        _require_running(resource, identity)
        console.ok(
            f"Гость {identity.vmid} {identity.name} запущен"
        )

    if identity.role == SETTINGS.infra_manager_role:
        _infra_manager_repair(node, repo_root, identity)
    else:
        console.ok(
            f"Гость {identity.vmid} {identity.name}: "
            "базовое безопасное восстановление завершено"
        )
    return 0


def _run_test(
    client: PveClient,
    repo_root: Path,
    identity: GuestIdentity,
) -> int:
    resource = _generic_status(
        client,
        repo_root,
        identity,
        announce=False,
    )
    verify_guest_status(repo_root, identity, resource)
    console.ok(
        f"Гость {identity.vmid} {identity.name}: расширенная проверка пройдена"
    )
    return 0


def _run_sync(
    repo_root: Path,
    identity: GuestIdentity,
) -> int:
    if identity.role == SETTINGS.infra_manager_role:
        _infra_manager_sync(repo_root, identity)
        return 0

    console.info(
        f"Синхронизация provision.yaml гостя {identity.vmid} "
        "без полного развёртывания"
    )
    return run_deploy_guest(
        repo_root,
        identity.vmid,
        phase="provision",
    )


def run_guest_operation(
    repo_root: Path,
    operation: str,
    vmid: int,
    *,
    show_secrets: bool = False,
) -> int:
    """Выполнить одну стандартную операцию над выбранным гостем."""

    if operation not in GUEST_OPERATIONS:
        raise InfraManagerError(f"Неизвестная операция гостя: {operation}")

    require_runtime_activation_idle()
    with project_checkout_lock(exclusive=False):
        identity = guest_identity(repo_root, vmid)

        if operation == "status":
            return _run_status(
                PveClient.from_opentofu_env(),
                repo_root,
                identity,
                show_secrets=show_secrets,
            )

        with guest_operation_lock(identity.vmid, operation):
            if operation == "deploy":
                return _run_deploy(
                    repo_root,
                    identity,
                    show_secrets=show_secrets,
                )
            if operation == "sync":
                return _run_sync(repo_root, identity)

            client = PveClient.from_opentofu_env()
            if operation == "repair":
                return _run_repair(client, repo_root, identity)
            if operation == "test":
                return _run_test(client, repo_root, identity)

    raise InfraManagerError(f"Неизвестная операция гостя: {operation}")
