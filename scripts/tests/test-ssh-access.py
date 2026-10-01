#!/usr/bin/env python3
"""Проверить, что отказ OTP не запускает удаление прежнего SSH-доступа."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.access import load_access_policy
from infra_manager.common import InfraManagerError
from infra_manager.semaphore import retire_machine_ssh_template
from infra_manager.ssh_access import verify_ssh_access


def test_failed_otp_keeps_legacy_access() -> None:
    policy = load_access_policy(ROOT)
    required = {
        vmid
        for edge in policy.machine_ssh_edges
        for vmid in (edge.source_vmid, edge.target_vmid)
    }
    running = {
        vmid: SimpleNamespace(record=policy.guests[vmid], address=policy.guests[vmid].address)
        for vmid in required
    }
    commands: list[list[str]] = []

    def remote(_guest: object, **kwargs: object) -> SimpleNamespace:
        command = kwargs["command"]
        assert isinstance(command, list)
        commands.append(command)
        return SimpleNamespace(returncode=1)

    with (
        patch("infra_manager.ssh_access.load_access_policy", return_value=policy),
        patch("infra_manager.ssh_access.PveClient"),
        patch("infra_manager.ssh_access._running_guests", return_value=running),
        patch("infra_manager.ssh_access._temporary_admin_identity", return_value=(Path("key"), Path("cert"))),
        patch("infra_manager.ssh_access._temporary_host_ca_file", return_value=Path("known_hosts")),
        patch("infra_manager.ssh_access._ssh", side_effect=remote),
    ):
        try:
            verify_ssh_access(ROOT, "pve")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Неисправный OTP должен остановить переход")

    assert commands
    assert not any("machine_ed25519" in str(command) for command in commands)
    assert not any("systemctl disable" in str(command) for command in commands)


def test_retire_template_without_host_project_id_file() -> None:
    deleted: list[tuple[str, str]] = []

    class Client:
        auth_mode = "cookie"

        def ensure_api_token(self) -> None:
            return None

        def get(self, path: str):
            if path == "/projects":
                return [{"id": 1, "name": "Proxmox Infrastructure"}]
            if path.startswith("/project/1/templates"):
                return [{"id": 6, "name": "Sync Machine SSH"}]
            raise AssertionError(path)

        def _request(self, method: str, path: str) -> None:
            deleted.append((method, path))

    with patch("infra_manager.semaphore.SemaphoreClient", return_value=Client()):
        retire_machine_ssh_template()
    assert deleted == [("DELETE", "/project/1/templates/6")]


if __name__ == "__main__":
    test_failed_otp_keeps_legacy_access()
    test_retire_template_without_host_project_id_file()
    print("SSH OTP cutover safety test passed.")
