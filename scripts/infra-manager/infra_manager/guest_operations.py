"""Единые штатные операции над гостями проекта."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from .common import (
    InfraManagerError,
    cancel_runtime_activation,
    console,
    require_runtime_activation_idle,
    reserve_runtime_activation,
)
from .guest_catalog import GuestIdentity, deployable_guests, guest_identity, load_manifest
from .guest_deploy import (
    project_branch_for_checkout,
    project_revision,
    run_deploy_guest,
)
from .guest_status import show_guest_status
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
    "sync",
)

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


def _supports_safe_sync(identity: GuestIdentity) -> bool:
    """Разрешить Sync только там, где есть отдельные безопасные задачи."""
    if identity.role == SETTINGS.infra_manager_role:
        return True
    provision = load_manifest(identity.directory / "provision.yaml")
    if identity.name == "web-gateway" and "web_gateway" in provision:
        return True
    if "ai_services" in provision:
        return True
    portal = provision.get("portal")
    local = portal.get("local") if isinstance(portal, dict) else None
    return isinstance(local, dict) and local.get("enabled") is True


def operation_guests(
    repo_root: Path,
    operation: str,
) -> tuple[GuestIdentity, ...]:
    """Вернуть гостей, для которых операция действительно поддерживается."""

    if operation not in GUEST_OPERATIONS:
        raise InfraManagerError(f"Неизвестная операция гостя: {operation}")

    guests = deployable_guests(repo_root)
    if operation == "sync":
        return tuple(guest for guest in guests if _supports_safe_sync(guest))
    return guests


def _expected_resource_type(repo_root: Path, identity: GuestIdentity) -> str:
    """Определить ожидаемый тип ресурса PVE по профилю гостя."""

    profile = identity.source.get("profile")
    if not isinstance(profile, str) or not profile:
        raise InfraManagerError(f"{identity.manifest}: не задан profile")

    defaults_path = repo_root / "infrastructure" / "guests" / "defaults.yaml"
    defaults = load_manifest(defaults_path)
    profiles = defaults.get("profiles")
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
    """Остановить действие, если PVE не подтверждает запуск выбранного гостя."""
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
    """Подтвердить принадлежность и состояние работающего гостя в PVE."""
    resource = _guest_resource(client, repo_root, identity)
    _require_running(resource, identity)
    if announce:
        console.ok(
            f"Гость {identity.vmid} {identity.name}: объект PVE запущен"
        )
    return resource


def _standalone_runtime() -> bool:
    """Определить независимую рабочую среду из доверенного запуска 910."""
    return os.environ.get("INFRA_RUNTIME_MODE") == "standalone"


def _infra_manager_repair(
    node: str,
    repo_root: Path,
    identity: GuestIdentity,
) -> None:
    """Восстановить служебный OpenBao и синхронизировать Semaphore.

        Это изменяющая операция: сначала обновляется доверенный помощник PVE,
        затем восстанавливаются доступы OpenBao, после чего синхронизируется
        Semaphore. Окончательная готовность проверяется отдельным status."""
    del identity
    if not _standalone_runtime():
        from .semaphore import sync_project_from_task

    console.info("Обновление PVE-only OpenBao helper")
    install_openbao_host_support(node, repo_root)
    console.info("Безопасное восстановление OpenBao")
    repair_openbao_on_host(node)
    if _standalone_runtime():
        console.ok(
            "OpenBao восстановлен; независимая рабочая среда "
            "проверяется отдельным Status Guest"
        )
        return
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
    """Согласовать Semaphore с выбранной версией проекта без восстановления OpenBao."""
    del identity
    if _standalone_runtime():
        console.ok(
            "Требуемая Git-версия инфраструктуры уже подготовлена "
            "общим операторским исполнителем; шаблоны Semaphore не используются"
        )
        return
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
    """Выполнить Deploy Guest и проверить итоговую готовность.

    Для самообновления управляющего гостя заранее резервируется окно
    активации. При ошибке развертывания резерв отменяется, но уже
    выполненные инфраструктурные изменения не откатываются автоматически.
    """

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

        # В самообновлении новый runtime ещё не активирован, а списки
        # Semaphore ещё не синхронизированы: готовность проверит
        # внешняя служба только после выхода этого процесса.
        if self_update:
            console.info(
                "Настройка управляющего гостя завершена; "
                ("активация и проверка независимой среды назначены "
                 if _standalone_runtime()
                 else "активация и синхронизация Semaphore назначены ")
                "после выхода текущего задания"
            )
            return 0

        # Обычный deploy печатает итоговый статус из доверенного PVE-оператора
        # либо из Semaphore без раскрытия секретов.
        if os.environ.get("INFRA_MANAGER_OPERATOR_FINAL_STATUS") == "1":
            return 0

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
    """Показать состояние гостя без его перенастройки.

        Показ секретов возможен только при явно разрешённом доверенном режиме."""
    console.info(
        f"Проверка состояния гостя {identity.vmid} — {identity.name}"
    )
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
    allow_pending_activation: bool = False,
) -> int:
    """Показать статус; при самообновлении разрешить только итог управляющего гостя."""

    if not allow_pending_activation:
        require_runtime_activation_idle()
    with project_checkout_lock(exclusive=False):
        identity = guest_identity(repo_root, vmid)
        if allow_pending_activation and identity.role != SETTINGS.infra_manager_role:
            raise InfraManagerError(
                "Проверка при отложенной активации разрешена только "
                "управляющему гостю"
            )
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
    """Запустить остановленного гостя и выполнить предусмотренное восстановление.

        Перед запуском объект подтверждается по VMID и описанию. Для
        infra-manager восстанавливаются OpenBao и Semaphore; у остальных
        гостей эта операция не переустанавливает приложения."""
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


def _run_sync(
    repo_root: Path,
    identity: GuestIdentity,
) -> int:
    """Применить только разрешённые изменения конфигурации гостя.

    Для infra-manager сохраняется синхронизация Semaphore, остальные
    используют отдельную безопасную фазу Ansible без обновления программ.
    """
    if identity.role == SETTINGS.infra_manager_role:
        _infra_manager_sync(repo_root, identity)
        return 0

    if not _supports_safe_sync(identity):
        raise InfraManagerError(
            f"Для гостя {identity.vmid} нет безопасных задач Sync; "
            "используйте infra-manager deploy после подготовки."
        )
    console.info(
        f"Безопасная синхронизация настроек гостя {identity.vmid} "
        "без установки программ и обновления Docker-образов"
    )
    return run_deploy_guest(
        repo_root,
        identity.vmid,
        phase="sync",
    )


def run_guest_operation(
    repo_root: Path,
    operation: str,
    vmid: int,
    *,
    show_secrets: bool = False,
) -> int:
    """Выполнить разрешённую операцию над указанным гостем.

    Новые операции не принимаются при активации управляющей среды.
    Изменяющие действия защищены блокировкой по VMID, чтобы два
    задания не меняли один объект одновременно. Status не изменяет
    гостя и выполняется без этой блокировки.
    """

    if operation not in GUEST_OPERATIONS:
        raise InfraManagerError(f"Неизвестная операция гостя: {operation}")

    require_runtime_activation_idle()
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

    raise InfraManagerError(f"Неизвестная операция гостя: {operation}")
