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
    print("[ОК] Ansible one-shot journal and isolated first-create stage")
