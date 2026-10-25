#!/usr/bin/env python3
"""Read-only native Ansible preparation gates, independent of real HDD."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "automation/ansible"


def test_preflight_has_no_disk_writes() -> None:
    path = ANSIBLE / "roles/physical_storage/tasks/prepare-preflight.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and len(tasks) >= 15
    allowed = {
        "ansible.builtin.assert", "ansible.builtin.stat",
        "ansible.builtin.command", "ansible.builtin.find",
        "ansible.builtin.set_fact", "ansible.builtin.debug",
    }
    commands = []
    for task in tasks:
        modules = [key for key in task if key.startswith("ansible.builtin.")]
        assert len(modules) == 1, task.get("name")
        assert modules[0] in allowed, task.get("name")
        if modules[0] == "ansible.builtin.command":
            assert task.get("changed_when") is False, task.get("name")
            command = task[modules[0]]["argv"]
            assert isinstance(command, list)
            assert command[0] in {"lsblk", "jq", "readlink", "wipefs", "blkid", "zpool"}
            if command[0] == "wipefs":
                assert "--no-act" in command
            if command[0] == "zpool":
                assert command[1] == "list"
            if command[0] == "blkid":
                assert command[1] == "-p"
            if command[0] == "readlink":
                assert command[1] == "--canonicalize-existing"
            commands.append(command[0])
    assert {"lsblk", "jq", "readlink", "wipefs", "blkid", "zpool"} <= set(commands)
    text = path.read_text(encoding="utf-8")
    for term in (
        "physical_disk_intent.fingerprint",
        "physical_disk_intent.items",
        "physical_preparation_saved",
        "physical_preparation_journal",
        "physical_preparation_by_id",
        "physical_preparation_wipefs",
        "physical_preparation_blkid",
        "reject_writes",
        "serial",
        "mountpoints",
        "physical_preparation_tree",
    ):
        assert term in text, term
    playbook = yaml.safe_load(
        (ANSIBLE / "playbooks/prepare-physical-storage-preflight.yml").read_text()
    )
    assert playbook[0]["gather_facts"] is False
    assert playbook[0]["tasks"][0]["ansible.builtin.include_role"] == {
        "name": "physical_storage", "tasks_from": "prepare-preflight.yml",
    }


def test_jq_refuses_system_disk() -> None:
    if not shutil.which("jq"):
        raise AssertionError("jq is required by NAS Ansible preflight")
    tasks = yaml.safe_load(
        (ANSIBLE / "roles/physical_storage/tasks/prepare-preflight.yml").read_text()
    )
    filters = [
        task["ansible.builtin.command"]["argv"][-1]
        for task in tasks if task.get("ansible.builtin.command", {}).get("argv", [None])[0] == "jq"
    ]
    assert len(filters) == 2
    inventory = {
        "blockdevices": [
            {
                "type": "disk", "path": "/dev/sda", "serial": "SYSTEM",
                "mountpoints": [None],
                "children": [{
                    "type": "part", "mountpoints": [None],
                    "children": [{"type": "lvm", "mountpoints": ["/"]}],
                }],
            },
            {
                "type": "disk", "path": "/dev/sdb", "serial": "NEW-DISK",
                "mountpoints": [None], "children": None,
            },
        ]
    }
    source = json.dumps(inventory)
    def jq(expression: str, *, serial: str | None = None) -> bool:
        args = ["jq", "-e"]
        if serial is not None:
            args.extend(["--arg", "serial", serial])
        args.append(expression)
        response = subprocess.run(
            args, input=source, text=True, capture_output=True, check=False,
        )
        return response.returncode == 0 and response.stdout.strip() == "true"
    assert jq(filters[0])
    assert jq(filters[1], serial="NEW-DISK")
    assert not jq(filters[1], serial="SYSTEM")
    inventory["blockdevices"][0]["children"] = []
    source = json.dumps(inventory)
    assert not jq(filters[0]), "Unknown root disk must refuse admission"


def test_preflight_runs_before_legacy_writer() -> None:
    source = (
        ROOT / "scripts/infra-manager/infra_manager/physical_nas_storage_prepare.py"
    ).read_text(encoding="utf-8")
    assert source.index('operation="prepare-preflight"') < (
        source.index('"python3 /usr/local/sbin/nas-storage-prepare"')
    )
    assert "if pending is None:" in source
    universal = (
        ROOT / "scripts/infra-manager/infra_manager/physical_host_ansible.py"
    ).read_text(encoding="utf-8")
    assert 'operation == "prepare-preflight"' in universal
    assert "prepare-physical-storage-preflight.yml" in universal


if __name__ == "__main__":
    test_preflight_has_no_disk_writes()
    test_jq_refuses_system_disk()
    test_preflight_runs_before_legacy_writer()
    print("[ОК] Ansible HDD preparation preflight is read-only and fail-closed")
