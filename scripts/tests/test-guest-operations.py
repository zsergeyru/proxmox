#!/usr/bin/env python3
"""Проверки общего механизма операций над гостями."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.access import GuestRecord
from infra_manager.common import InfraManagerError
from infra_manager.guest_catalog import GuestIdentity
from infra_manager import guest_operations as module


def identity(vmid: int = 410, role: str = "ai-control") -> GuestIdentity:
    root = Path("/tmp/guest")
    return GuestIdentity(
        vmid=vmid,
        name="guest",
        role=role,
        directory=root,
        manifest=root / "guest.yaml",
        source={"vmid": vmid, "name": "guest", "role": role},
    )


def record(vmid: int = 410, kind: str = "vm") -> GuestRecord:
    return GuestRecord(
        vmid=vmid,
        name="guest",
        kind=kind,
        node="pve",
        address="192.0.2.10",
        subnet="192.0.2.0/24",
        pve_management=True,
        provisioned_linux=True,
    )


def test_status_guest() -> None:
    client = SimpleNamespace(
        find_vm=lambda vmid: {
            "vmid": vmid,
            "type": "qemu",
            "name": "guest",
            "node": "pve",
            "status": "running",
        }
    )
    with (
        patch.object(module, "guest_identity", return_value=identity()),
        patch.object(module, "_guest_record", return_value=record()),
        patch.object(module, "PveClient", return_value=client),
        patch.dict(module.STATUS_HANDLERS, {}, clear=True),
    ):
        assert module.status_guest(Path("/repo"), 410) == 0


def test_repair_starts_stopped_guest() -> None:
    run_task = Mock()
    client = SimpleNamespace(
        find_vm=lambda vmid: {
            "vmid": vmid,
            "type": "qemu",
            "name": "guest",
            "node": "pve",
            "status": "stopped",
        },
        run_task=run_task,
    )
    with (
        patch.object(module, "guest_identity", return_value=identity()),
        patch.object(module, "_guest_record", return_value=record()),
        patch.object(module, "PveClient", return_value=client),
        patch.object(module, "status_guest", return_value=0) as status,
    ):
        assert module.repair_guest(Path("/repo"), 410) == 0

    run_task.assert_called_once_with(
        "POST",
        "/nodes/pve/qemu/410/status/start",
        node="pve",
    )
    status.assert_called_once_with(Path("/repo"), 410)


def test_guest_checks_management_address() -> None:
    client = SimpleNamespace(
        guest_management_ipv4=lambda **kwargs: "192.0.2.10"
    )
    with (
        patch.object(module, "status_guest", return_value=0),
        patch.object(module, "_guest_record", return_value=record()),
        patch.object(module, "PveClient", return_value=client),
    ):
        assert module.test_guest(Path("/repo"), 410) == 0


def test_current_repo_branch() -> None:
    with patch.object(
        module,
        "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="feature/test\n",
            stderr="",
        ),
    ):
        assert module._current_repo_branch(Path("/repo")) == "feature/test"

    with (
        patch.object(
            module,
            "run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="",
            ),
        ),
        patch.object(module.SETTINGS, "project_branch", return_value="main"),
    ):
        assert module._current_repo_branch(Path("/repo")) == "main"


def test_sync_dispatches_by_role() -> None:
    calls: list[tuple[Path, int]] = []

    def sync_handler(repo_root: Path, guest: GuestIdentity) -> int:
        calls.append((repo_root, guest.vmid))
        return 0

    with (
        patch.object(module, "guest_identity", return_value=identity()),
        patch.object(module, "_guest_record", return_value=record()),
        patch.dict(
            module.SYNC_HANDLERS,
            {"ai-control": sync_handler},
            clear=True,
        ),
    ):
        assert module.sync_guest(Path("/repo"), 410) == 0
    assert calls == [(Path("/repo"), 410)]

    with (
        patch.object(module, "guest_identity", return_value=identity()),
        patch.object(module, "_guest_record", return_value=record()),
        patch.dict(module.SYNC_HANDLERS, {}, clear=True),
    ):
        try:
            module.sync_guest(Path("/repo"), 410)
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Sync без обработчика роли должен завершаться ошибкой")


def test_deploy_reserves_self_update_activation() -> None:
    infra = identity(vmid=920, role=module.SETTINGS.infra_manager_role)
    with (
        patch.object(module, "require_runtime_activation_idle"),
        patch.object(module, "guest_identity", return_value=infra),
        patch.object(module, "reserve_runtime_activation") as reserve,
        patch.object(module, "cancel_runtime_activation") as cancel,
        patch.object(module, "run_deploy_guest", return_value=0) as deploy,
    ):
        assert module.deploy_guest(Path("/repo"), 920) == 0

    reserve.assert_called_once_with()
    cancel.assert_not_called()
    deploy.assert_called_once_with(
        Path("/repo"),
        920,
        bootstrap_scope=False,
        phase="all",
    )


def test_operation_dispatch() -> None:
    with (
        patch.object(module, "require_runtime_activation_idle"),
        patch.object(module, "status_guest", return_value=0) as status,
    ):
        assert module.run_guest_operation(Path("/repo"), 410, "status") == 0
    status.assert_called_once_with(Path("/repo"), 410)

    try:
        module.run_guest_operation(Path("/repo"), 410, "unknown")
    except InfraManagerError:
        pass
    else:
        raise AssertionError("Неизвестная операция должна отклоняться")


def main() -> None:
    test_status_guest()
    test_repair_starts_stopped_guest()
    test_guest_checks_management_address()
    test_current_repo_branch()
    test_sync_dispatches_by_role()
    test_deploy_reserves_self_update_activation()
    test_operation_dispatch()
    print("[ОК] Общий механизм операций над гостями проверен")


if __name__ == "__main__":
    main()
