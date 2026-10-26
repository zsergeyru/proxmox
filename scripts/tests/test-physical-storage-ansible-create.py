#!/usr/bin/env python3
"""Contracts for staged native Ansible disk creation without production cutover."""
from __future__ import annotations

from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "automation/ansible/roles/physical_storage/tasks"


def read(name: str):
    result = yaml.safe_load((ROLE / name).read_text(encoding="utf-8"))
    assert isinstance(result, list) and result
    return result


def test_one_shot_journal() -> None:
    tasks = read("prepare-journal.yml")
    copies = [t["ansible.builtin.copy"] for t in tasks
              if "ansible.builtin.copy" in t]
    assert len(copies) == 1
    copy = copies[0]
    assert copy["dest"] == "/etc/nas-apps/storage-preparation.json"
    assert copy["force"] is False
    assert copy["follow"] is False
    assert copy["unsafe_writes"] is False
    assert copy["mode"] == "0600"
    assert copy["owner"] == "root"
    assert "'state': 'started'" in copy["content"]
    assert "physical_disk_intent['items']" in copy["content"]
    assert "physical_preparation_journal_written is changed" in str(tasks)
    assert "physical_preparation_old_result" in str(tasks)
    assert "physical_preparation_saved" in str(tasks)
    assert "physical_preparation_journal_committed" in str(tasks)
    assert "ansible.builtin.slurp" in str(tasks)
    assert all("ansible.builtin.shell" not in t for t in tasks)


def test_write_stage_is_gated_and_not_connected() -> None:
    tasks = read("prepare-write.yml")
    assert tasks[0]["ansible.builtin.assert"]
    assert "physical_preparation_journal_committed" in str(tasks[0])
    assert "selectattr('action', 'equalto', 'preserve')" in str(tasks[0])
    commands = [t["ansible.builtin.command"]["argv"] for t in tasks
                if "ansible.builtin.command" in t]
    destructive = [
        a for a in commands if a[0] in ("sgdisk", "mkfs.ext4")
        or a[:2] == ["zpool", "create"]
    ]
    assert len(destructive) == 3
    assert destructive[0][0] == "sgdisk"
    assert destructive[1][0] == "mkfs.ext4"
    assert destructive[2][:2] == ["zpool", "create"]
    assert "--new=1:1MiB:0" in destructive[0]
    assert "-L" in destructive[1]
    assert "ashift=12" in destructive[2]
    assert all("physical_preparation_by_id" in str(x) for x in destructive)
    assert "physical_preparation_final_targets" in str(tasks)
    assert "physical_preparation_zfs_dirs" in str(tasks)
    assert "physical_preparation_zfs_content" in str(tasks)
    assert all(
        "ansible.builtin.shell" not in task and
        "ansible.builtin.script" not in task and
        "ansible.builtin.raw" not in task
        for task in tasks
    )

    # No supported deployment/status/repair path may write disks through
    # these unfinished roles until OMV registration, recovery and durable
    # controller-side identity have been implemented and tested.
    playbooks = ROOT / "automation/ansible/playbooks"
    for p in playbooks.glob("*.yml"):
        source = p.read_text(encoding="utf-8")
        assert "tasks_from: prepare-write.yml" not in source, p
        assert "tasks_from: prepare-journal.yml" not in source, p
    for p in (ROOT / "automation/ansible/roles/nas/tasks").glob("*.yml"):
        source = p.read_text(encoding="utf-8")
        assert "tasks_from: prepare-write.yml" not in source, p
        assert "tasks_from: prepare-journal.yml" not in source, p


def test_preserved_ext4_is_checked_without_writes() -> None:
    preserved = read("prepare-preserved-ext4.yml")
    assert "physical_preserved_ext4_items" in str(preserved)
    assert "physical_preserved_ext4_mounts" in str(preserved)
    assert "physical_preserved_ext4_sources" in str(preserved)
    assert "filesystem_uuid" in str(preserved)
    for task in preserved:
        assert not any(key in task for key in (
            "ansible.builtin.shell", "ansible.builtin.raw",
            "ansible.builtin.script", "ansible.builtin.copy",
        ))
        if "ansible.builtin.command" in task:
            assert task["changed_when"] is False
            assert task["ansible.builtin.command"]["argv"][0] in {
                "blkid", "findmnt", "readlink",
            }


def test_recovery_is_strictly_read_only() -> None:
    tasks = read("prepare-recover-inspect.yml")
    assert len(tasks) >= 10
    assert "physical_recovery_journal_stat" in str(tasks)
    assert "physical_recovery_journal_bytes" in str(tasks)
    assert "physical_recovery_journal.fingerprint == physical_disk_intent.fingerprint" in str(tasks)
    assert "physical_recovery_journal['items'] == physical_disk_intent['items']" in str(tasks)
    assert "physical_recovery_ext4" in str(tasks)
    assert "physical_recovery_guids" in str(tasks)
    assert "physical_recovery_zpool_status" in str(tasks)
    for task in tasks:
        for forbidden in (
            "ansible.builtin.shell", "ansible.builtin.script",
            "ansible.builtin.raw", "ansible.builtin.copy",
            "ansible.builtin.file", "ansible.builtin.mount",
        ):
            assert forbidden not in task, task.get("name")
        if "ansible.builtin.command" in task:
            assert task.get("changed_when") is False
            argv = task["ansible.builtin.command"]["argv"]
            assert argv[0] in {"lsblk", "jq", "blkid", "zpool"}
            if argv[0] == "zpool":
                assert argv[1] in {"status", "get"}
    # Only the dedicated *read-only* inspection playbook may use this
    # role. In particular, the journal creator and filesystem writer
    # must remain disconnected from recovery and normal deployment.
    for p in (ROOT / "automation/ansible/playbooks").glob("*.yml"):
        source = p.read_text(encoding="utf-8")
        document = yaml.safe_load(source)
        active = [
            task.get("ansible.builtin.include_role", {}).get("tasks_from")
            for play in document if isinstance(play, dict)
            for task in play.get("tasks", []) if isinstance(task, dict)
        ]
        assert "prepare-write.yml" not in active, p
        assert "prepare-journal.yml" not in active, p
        if "prepare-recover-inspect.yml" in active:
            assert p.name in {
                "inspect-physical-storage-recovery.yml",
                "inspect-nas-storage-recovery.yml",
            }, p
    entry = (ROOT / "automation/ansible/playbooks/inspect-physical-storage-recovery.yml")
    assert "tasks_from: prepare-recover-inspect.yml" in entry.read_text(encoding="utf-8")


def test_omv_recovery_audit_is_read_only() -> None:
    audit = yaml.safe_load(
        (ROOT / "automation/ansible/roles/nas/tasks/prepare-omv-audit.yml").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(audit, list) and len(audit) >= 7
    for task in audit:
        modules = [key for key in task if key.startswith("ansible.builtin.")]
        assert len(modules) == 1, task.get("name")
        assert modules[0] in {
            "ansible.builtin.assert", "ansible.builtin.set_fact",
            "ansible.builtin.command", "ansible.builtin.debug",
        }, task.get("name")
        if "ansible.builtin.command" in task:
            assert task.get("changed_when") is False
            assert task["ansible.builtin.command"]["argv"] == [
                "omv-confdbadm", "read", "conf.system.filesystem.mountpoint",
            ]
    assert "nas_omv_existing_mounts" in str(audit)
    assert "by_directory" in str(audit)
    assert "by_source" in str(audit)
    assert "physical_recovery_journal.fingerprint == physical_disk_intent.fingerprint" in str(audit)
    playbook = yaml.safe_load(
        (ROOT / "automation/ansible/playbooks/inspect-nas-storage-recovery.yml").read_text(
            encoding="utf-8"
        )
    )
    assert len(playbook[0]["tasks"]) == 2
    assert [
        task["ansible.builtin.include_role"]["tasks_from"]
        for task in playbook[0]["tasks"]
    ] == ["prepare-recover-inspect.yml", "prepare-omv-audit.yml"]
    assert [
        task["ansible.builtin.include_role"]["name"]
        for task in playbook[0]["tasks"]
    ] == ["physical_storage", "nas"]


def test_item_key_never_collides_with_dict_method() -> None:
    preflight = read("prepare-preflight.yml")
    journal = read("prepare-journal.yml")
    writer = read("prepare-write.yml")
    for name, tasks in (("preflight", preflight), ("journal", journal), ("writer", writer)):
        assert "physical_disk_intent.items" not in str(tasks), name
    template = Environment(undefined=StrictUndefined).from_string(
        "{{ physical_disk_intent['items'] | length }}"
    )
    assert template.render(physical_disk_intent={
        "items": [{"role": "shared", "serial": "A", "action": "create-ext4"}],
    }) == "1"


if __name__ == "__main__":
    test_one_shot_journal()
    test_write_stage_is_gated_and_not_connected()
    test_item_key_never_collides_with_dict_method()
    test_preserved_ext4_is_checked_without_writes()
    test_recovery_is_strictly_read_only()
    test_omv_recovery_audit_is_read_only()
    print("[ОК] Ansible one-shot journal and isolated first-create stage")
