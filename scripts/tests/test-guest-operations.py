#!/usr/bin/env python3
"""Проверки стандартных операций над гостями."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import guest_operations as operations
from infra_manager import semaphore as semaphore_module
from infra_manager.common import InfraManagerError
from infra_manager.guest_catalog import deployable_guests, guest_identity


def fail(message: str) -> None:
    raise SystemExit(message)


def check_operation_catalog() -> None:
    expected = {guest.vmid for guest in deployable_guests(ROOT)}
    for operation in ("deploy", "status", "repair", "test"):
        actual = {
            guest.vmid
            for guest in operations.operation_guests(ROOT, operation)
        }
        if actual != expected:
            fail(
                f"Операция {operation} должна использовать общий каталог гостей"
            )

    actual_sync = {
        guest.vmid
        for guest in operations.operation_guests(ROOT, "sync")
    }
    if actual_sync != expected:
        fail("Sync Guest должен использовать общий каталог гостей")


class FakePveClient:
    def __init__(self, resource: dict[str, object]) -> None:
        self.resource = resource
        self.actions: list[tuple[str, str, str]] = []

    def find_vm(self, vmid: int):
        if str(self.resource.get("vmid")) != str(vmid):
            return None
        return dict(self.resource)

    def run_task(
        self,
        method: str,
        endpoint: str,
        *,
        node: str,
        **kwargs: object,
    ) -> None:
        del kwargs
        self.actions.append((method, endpoint, node))
        if endpoint.endswith("/status/start"):
            self.resource["status"] = "running"


def check_status_operation() -> None:
    cases = (
        (
            guest_identity(ROOT, 109),
            {
                "vmid": 109,
                "name": "network-gateway",
                "type": "qemu",
                "node": "pve",
                "status": "running",
            },
        ),
        (
            guest_identity(ROOT, 910),
            {
                "vmid": 910,
                "name": "infra-manager",
                "type": "lxc",
                "node": "pve",
                "status": "running",
            },
        ),
    )

    for identity, resource in cases:
        client = FakePveClient(resource)
        with (
            patch.object(operations, "show_guest_status") as show,
            patch.object(
                operations,
                "project_branch_for_checkout",
                return_value="feature/unified-guest-status",
            ),
            patch.object(
                operations,
                "project_revision",
                return_value="abc1234",
            ),
        ):
            if operations._run_status(client, ROOT, identity) != 0:
                fail(
                    f"Status Guest должен завершаться успешно для {identity.vmid}"
                )
        show.assert_called_once()
        args, kwargs = show.call_args
        if args[:3] != (ROOT, identity, resource):
            fail("Status Guest передал неверные данные общему выводу")
        if kwargs != {
            "full": True,
            "show_secrets": True,
            "project_branch": "feature/unified-guest-status",
            "project_revision": "abc1234",
        }:
            fail(
                "Операторский Status Guest должен показывать полный статус "
                "и версию проекта"
            )

    stopped = FakePveClient(
        {
            "vmid": 109,
            "name": "network-gateway",
            "type": "qemu",
            "node": "pve",
            "status": "stopped",
        }
    )
    try:
        operations._run_status(stopped, ROOT, guest_identity(ROOT, 109))
    except InfraManagerError:
        pass
    else:
        fail("Status Guest должен считать остановленный гость ошибкой")

def check_repair_operation() -> None:
    identity = guest_identity(ROOT, 109)
    client = FakePveClient(
        {
            "vmid": 109,
            "name": "network-gateway",
            "type": "qemu",
            "node": "pve",
            "status": "stopped",
        }
    )
    if operations._run_repair(client, ROOT, identity) != 0:
        fail("Repair Guest не завершился после безопасного запуска гостя")
    if client.actions != [
        ("POST", "/nodes/pve/qemu/109/status/start", "pve")
    ]:
        fail(f"Repair Guest выполнил неожиданные действия: {client.actions!r}")


def check_infra_manager_repair_operation() -> None:
    identity = guest_identity(ROOT, 910)
    client = FakePveClient(
        {
            "vmid": 910,
            "name": "infra-manager",
            "type": "lxc",
            "node": "pve",
            "status": "running",
        }
    )
    with (
        patch.object(
            operations,
            "install_openbao_host_support",
        ) as install_host,
        patch.object(
            operations,
            "repair_openbao_on_host",
        ) as repair_openbao,
        patch.object(
            operations,
            "project_branch_for_checkout",
            return_value="feature/guest-portals",
        ) as project_branch,
        patch.object(
            semaphore_module,
            "sync_project_from_task",
        ) as sync_project,
    ):
        if operations._run_repair(client, ROOT, identity) != 0:
            fail("Repair Guest infra-manager должен завершаться успешно")

    install_host.assert_called_once_with("pve", ROOT)
    repair_openbao.assert_called_once_with("pve")
    project_branch.assert_called_once_with(ROOT)
    sync_project.assert_called_once_with(
        branch="feature/guest-portals",
        repo_root=ROOT,
    )


def check_test_operation() -> None:
    identity = guest_identity(ROOT, 109)
    resource = {
        "vmid": 109,
        "name": "network-gateway",
        "type": "qemu",
        "node": "pve",
        "status": "running",
    }
    client = FakePveClient(resource)

    with patch.object(
        operations,
        "verify_guest_status",
    ) as verify:
        if operations._run_test(client, ROOT, identity) != 0:
            fail("Test Guest должен завершаться успешно")

    verify.assert_called_once_with(ROOT, identity, resource)

def check_sync_operation() -> None:
    identity = guest_identity(ROOT, 910)
    with patch.object(
        operations,
        "_infra_manager_sync",
    ) as sync:
        if operations._run_sync(ROOT, identity) != 0:
            fail("Sync Guest infra-manager должен завершаться успешно")
    sync.assert_called_once_with(ROOT, identity)

    regular = guest_identity(ROOT, 109)
    with patch.object(
        operations,
        "run_deploy_guest",
        return_value=0,
    ) as deploy:
        if operations._run_sync(ROOT, regular) != 0:
            fail("Sync Guest обычного гостя должен завершаться успешно")
    deploy.assert_called_once_with(ROOT, 109, phase="provision")


def check_deploy_operation() -> None:
    identity = guest_identity(ROOT, 410)
    resource = {
        "vmid": 410,
        "name": "ai-control",
        "type": "qemu",
        "node": "pve",
        "status": "running",
    }
    client = FakePveClient(resource)

    with (
        patch.object(
            operations,
            "run_deploy_guest",
            return_value=0,
        ) as deploy,
        patch.object(
            operations.PveClient,
            "from_opentofu_env",
            return_value=client,
        ),
        patch.object(operations, "show_guest_status") as show,
        patch.object(
            operations,
            "project_branch_for_checkout",
            return_value="feature/unified-guest-status",
        ),
        patch.object(
            operations,
            "project_revision",
            return_value="abc1234",
        ),
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
    ):
        if operations._run_deploy(ROOT, identity) != 0:
            fail("Deploy Guest должен возвращать результат общего deploy")
    deploy.assert_called_once_with(ROOT, 410)
    show.assert_called_once()
    args, kwargs = show.call_args
    if args[:3] != (ROOT, identity, resource):
        fail("Deploy Guest передал неверные данные общему выводу")
    if kwargs != {
        "full": True,
        "show_secrets": True,
        "project_branch": "feature/unified-guest-status",
        "project_revision": "abc1234",
    }:
        fail(
            "Успешный Deploy Guest должен показывать операторский итог "
            "и версию проекта"
        )
    reserve.assert_not_called()
    cancel.assert_not_called()

    infra = guest_identity(ROOT, 910)
    with (
        patch.object(
            operations,
            "run_deploy_guest",
            return_value=1,
        ),
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
    ):
        if operations._run_deploy(ROOT, infra) != 1:
            fail("Deploy Guest должен сохранять код ошибки самообновления")
    reserve.assert_called_once_with()
    cancel.assert_called_once_with()

def check_dispatch() -> None:
    with (
        patch.object(operations, "require_runtime_activation_idle") as idle,
        patch.object(
            operations,
            "guest_identity",
            return_value=SimpleNamespace(
                vmid=109,
                name="network-gateway",
                role="network-gateway",
            ),
        ),
        patch.object(
            operations.PveClient,
            "from_opentofu_env",
            return_value=SimpleNamespace(),
        ),
        patch.object(operations, "_run_status", return_value=0) as status,
    ):
        if operations.run_guest_operation(ROOT, "status", 109) != 0:
            fail("Диспетчер Status Guest вернул ошибку")
    idle.assert_called_once_with()
    status.assert_called_once()


def main() -> None:
    check_operation_catalog()
    check_status_operation()
    check_repair_operation()
    check_infra_manager_repair_operation()
    check_test_operation()
    check_sync_operation()
    check_deploy_operation()
    check_dispatch()
    print("[ОК] Стандартные операции над гостями проверены")


if __name__ == "__main__":
    main()
