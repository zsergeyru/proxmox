#!/usr/bin/env python3
"""Контрактные проверки ограниченного канала 410 → PVE → 910."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager.pve_host import _agent_operator_policy


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ssh = load_module("agent_ssh", ROOT / "scripts/infra-manager/host/agent_ssh.py")
agent = load_module("agent_operator", ROOT / "scripts/infra-manager/host/agent_operator.py")
manager = load_module("host_manager_agent", ROOT / "scripts/infra-manager/host/manager.py")


def denied(action, expected_exception):
    try:
        action()
    except expected_exception:
        return
    raise AssertionError("Запрещённая операция была принята")


def test_command_parsing() -> None:
    assert ssh.parse_request("infra-manager guests") == ["guests"]
    assert ssh.parse_request("infra-manager status") == ["status"]
    assert ssh.parse_request("infra-manager status 340") == ["status", "340"]
    for command in (
        "bash", "infra-manager", "infra-manager recover",
        "infra-manager deploy", "infra-manager repair",
        "infra-manager status 340; id",
        "infra-manager status 340 && cat /etc/shadow",
        "infra-manager status $(id)", "infra-manager status 340\nwhoami",
        "infra-manager status -1", "infra-manager status ../passwd",
    ):
        denied(lambda command=command: ssh.parse_request(command), ValueError)


def test_allowlist() -> None:
    permitted = _agent_operator_policy(ROOT)["allowed"]
    assert permitted["guests"] is True
    assert permitted["status"] == "all"
    assert agent.validate_command(["guests"], permitted) == ("guests", None)
    assert agent.validate_command(["status"], permitted) == ("status", None)
    assert agent.validate_command(["status", "910"], permitted) == ("status", 910)
    managed = permitted["deploy"]
    assert managed
    assert set(managed).isdisjoint({100, 410, 910})
    for operation in ("test", "deploy", "sync", "repair"):
        assert permitted[operation] == managed
        assert agent.validate_command([operation, str(managed[0])], permitted) == (
            operation, managed[0]
        )
        for vmid in (100, 410, 910, 999999):
            denied(
                lambda operation=operation, vmid=vmid: agent.validate_command(
                    [operation, str(vmid)], permitted
                ),
                agent.AgentAccessError,
            )
    for arguments in (
        [], ["recover"], ["repair"], ["status", "-1"],
        ["status", "340", ";"], ["deploy", "340;id"],
        ["guests", "--full"], ["sync", "999999"],
    ):
        denied(lambda arguments=arguments: agent.validate_command(arguments, permitted), agent.AgentAccessError)


def test_one_use_approval() -> None:
    permitted = _agent_operator_policy(ROOT)["allowed"]
    vmid = permitted["deploy"][0]
    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.object(agent, "APPROVALS", Path(tmp)),
            patch.object(agent, "_safe_root_file"),
            patch.object(agent, "_secure_directory"),
            patch.object(agent, "audit"),
        ):
            agent.issue_approval("deploy", vmid, permitted)
            agent.consume_approval("deploy", vmid)
            denied(
                lambda: agent.consume_approval("deploy", vmid),
                FileNotFoundError,
            )
            agent.issue_approval("deploy", vmid, permitted)
            approval = Path(tmp) / f"deploy-{vmid}.json"
            approval.write_text(json.dumps({"expires_at": 0}), encoding="utf-8")
            denied(
                lambda: agent.consume_approval("deploy", vmid),
                agent.AgentAccessError,
            )
            assert not approval.exists()
            denied(
                lambda: agent.issue_approval("deploy", 910, permitted),
                agent.AgentAccessError,
            )


def test_agent_never_receives_trusted_flag() -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    with (
        patch.object(manager, "manager_identity", return_value=(910, "infra-manager")),
        patch.object(manager, "verify_manager"),
        patch.object(manager, "guest_running", return_value=True),
        patch.object(manager, "run", side_effect=fake_run),
    ):
        assert manager.proxy_to_manager(["status", "340"], trusted=False) == 0
        assert len(calls) == 1
        assert "--trusted-pve" not in calls[0]
        assert "/usr/local/sbin/infra-manager" in calls[0]
        calls.clear()
        assert manager.proxy_to_manager(["status", "340"]) == 0
        assert "--trusted-pve" in calls[0]


def main() -> None:
    test_command_parsing()
    test_allowlist()
    test_one_use_approval()
    test_agent_never_receives_trusted_flag()
    print("PVE agent access tests passed.")


if __name__ == "__main__":
    main()
