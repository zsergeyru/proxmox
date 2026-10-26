#!/usr/bin/env python3
"""Contract checks for generic physical-host Ansible and disk read-only gates."""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_host_ansible import run_physical_ansible
from infra_manager.physical_hosts import load_physical_host
from infra_manager.physical_host_operations import run_physical_host_operation


def refuse(fn) -> None:
    try:
        fn()
    except InfraManagerError:
        return
    raise AssertionError("Unsafe request was not refused")


def test_generic_ansible_transport() -> None:
    host = load_physical_host(ROOT, "nas")
    session = SimpleNamespace(
        host=host,
        ansible_parameters=lambda: ("/run/private/temporary-key", "-F /dev/null"),
    )
    calls = []

    def record(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=0)

    with patch("infra_manager.physical_host_ansible.run", side_effect=record):
        run_physical_ansible(
            ROOT, session, operation="inspect",
            variables={"physical_host_name": "nas"},
        )
        run_physical_ansible(
            ROOT, session, phase="base",
            variables={"physical_host_name": "nas"},
            callback="infra_nas_compact",
        )
        run_physical_ansible(
            ROOT, session, operation="recover-inspect",
            variables={"physical_host_name": "nas"},
        )
    assert len(calls) == 3
    assert calls[0][0][0] == "ansible-playbook"
    assert calls[0][0][1].endswith("/inspect-physical-storage.yml")
    assert calls[1][0][1].endswith("/deploy-nas.yml")
    assert calls[2][0][1].endswith("/inspect-nas-storage-recovery.yml")
    assert calls[0][1]["env"]["ANSIBLE_HOST_KEY_CHECKING"] == "True"
    assert "/run/private/temporary-key" in calls[0][0]
    assert "ansible_ssh_common_args" in calls[0][0][-1]
    refuse(lambda: run_physical_ansible(
        ROOT, session, phase="untrusted",
        variables={"physical_host_name": "nas"},
    ))
    refuse(lambda: run_physical_ansible(
        ROOT, session, operation="deploy",
        variables={"physical_host_name": "nas"},
    ))
    refuse(lambda: run_physical_ansible(
        ROOT, session, operation="inspect", phase="base",
        variables={"physical_host_name": "nas"},
    ))
    refuse(lambda: run_physical_ansible(
        ROOT, session, phase="base", callback="../unsafe",
        variables={"physical_host_name": "nas"},
    ))
    refuse(lambda: run_physical_ansible(
        ROOT, session, operation="recover-inspect", phase="services",
        variables={"physical_host_name": "nas"},
    ))
    refuse(lambda: run_physical_ansible(
        ROOT, session, operation="prepare-write",
        variables={"physical_host_name": "nas"},
    ))


def test_native_disk_read_only_contract() -> None:
    directory = ROOT / "automation/ansible"
    inspect = yaml.safe_load(
        (directory / "roles/physical_storage/tasks/inspect.yml").read_text()
    )
    assert isinstance(inspect, list) and len(inspect) >= 5
    commands = [task["ansible.builtin.command"] for task in inspect
                if "ansible.builtin.command" in task]
    assert len(commands) == 1
    assert commands[0]["argv"][0] == "lsblk"
    assert all("changed_when" in task and task["changed_when"] is False
               for task in inspect if "ansible.builtin.command" in task)
    assert all(not any(k in task for k in (
        "ansible.builtin.shell", "ansible.builtin.script",
        "ansible.builtin.raw")) for task in inspect)
    assert "reject_writes" in str(inspect)
    assert "item.serial is not none" in str(inspect)

    p = yaml.safe_load(
        (directory / "playbooks/deploy-nas.yml").read_text()
    )
    preflight = p[0]["pre_tasks"]
    assert preflight[0]["ansible.builtin.include_role"] == {
        "name": "physical_storage", "tasks_from": "inspect.yml",
    }
    assert preflight[1]["ansible.builtin.include_role"] == {
        "name": "physical_storage", "tasks_from": "verified.yml",
    }
    assert preflight[2]["ansible.builtin.include_role"] == {
        "name": "nas", "tasks_from": "verified-storage.yml",
    }
    verified = yaml.safe_load(
        (directory / "roles/physical_storage/tasks/verified.yml").read_text()
    )
    assert "physical_confirmed_state" in str(verified)
    assert "delegate_to" in str(verified)
    assert "pool_guid" in str(verified)
    assert "filesystem_uuid" in str(verified)
    assert all("changed_when" in item and item["changed_when"] is False
               for item in verified if "ansible.builtin.command" in item)
    generic = yaml.safe_load(
        (directory / "playbooks/inspect-physical-storage.yml").read_text()
    )
    assert generic[0]["gather_facts"] is False
    assert generic[0]["tasks"][0]["ansible.builtin.include_role"]["name"] == "physical_storage"


def test_initial_ssh_trust_is_generic() -> None:
    with (
        patch("infra_manager.physical_host_operations.resolve_saved_host", side_effect=lambda h: h),
        patch("infra_manager.physical_host_operations.require_host_access"),
        patch("infra_manager.physical_host_operations.bootstrap_physical_host") as bootstrap,
        patch("infra_manager.physical_host_operations.sys.stdin", io.StringIO("a-secret\n")),
        patch.dict(os.environ, {"INFRA_PVE_NODE": "pve"}, clear=False),
    ):
        assert run_physical_host_operation(ROOT, "bootstrap", "nas") == 0
        args = bootstrap.call_args.args
        assert args[0].name == "nas"
        assert args[1] == "pve"
        assert args[2] == "a-secret\n"
    # Invalid or unterminated secret must never be passed to SSH bootstrap.
    with (
        patch("infra_manager.physical_host_operations.resolve_saved_host", side_effect=lambda h: h),
        patch("infra_manager.physical_host_operations.require_host_access"),
        patch("infra_manager.physical_host_operations.bootstrap_physical_host") as bootstrap,
        patch("infra_manager.physical_host_operations.sys.stdin", io.StringIO("bad")),
        patch.dict(os.environ, {"INFRA_PVE_NODE": "pve"}, clear=False),
    ):
        refuse(lambda: run_physical_host_operation(ROOT, "bootstrap", "nas"))
        bootstrap.assert_not_called()


def test_driver_is_not_used_for_schema_or_inspect() -> None:
    parser = (
        ROOT / "scripts/infra-manager/infra_manager/physical_hosts.py"
    ).read_text()
    controller = (
        ROOT / "scripts/infra-manager/infra_manager/physical_host_operations.py"
    ).read_text()
    assert "_validate_physical_layout(storage)" in parser
    assert "validate_driver_storage" not in parser
    assert 'if operation == "inspect":' not in controller
    assert '"inspect"' not in controller.split("SUPPORTED_HOST_OPERATIONS =", 1)[1].splitlines()[0]
    assert "physical_nas" not in controller
    assert "omv8" not in controller


if __name__ == "__main__":
    test_generic_ansible_transport()
    test_native_disk_read_only_contract()
    test_driver_is_not_used_for_schema_or_inspect()
    test_initial_ssh_trust_is_generic()
    print("[ОК] Universal Ansible physical host and read-only disk preflight")
