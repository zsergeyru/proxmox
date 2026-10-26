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


def test_omv_native_registration_is_safe_and_disconnected() -> None:
    """OMV receives only new, verified filesystem records (not passwords or uuid)."""
    role = read_omv_register()
    source = (
        ROOT / "automation/ansible/roles/nas/tasks/prepare-omv-register.yml"
    ).read_text(encoding="utf-8")
    assert len(role) >= 10
    assert "physical_recovery_journal['items'] == physical_disk_intent['items']" in source
    assert "nas_omv_registration_authorized" in source
    assert "physical_recovery_ext4.results | length == 1" in source
    assert "physical_recovery_guids.results | length == 2" in source
    assert "physical_preparation_saved" not in source
    assert "nas_omv_registration_controller_state" in source
    assert "nas_omv_registration_before.stdout | from_json" in source
    command_tasks = [task for task in role if "ansible.builtin.command" in task]
    writes = [
        task for task in command_tasks
        if task["ansible.builtin.command"]["argv"][1] == "update"
    ]
    assert len(writes) == 1
    writer = writes[0]
    assert writer["ansible.builtin.command"]["argv"][:3] == [
        "omv-confdbadm", "update", "conf.system.filesystem.mountpoint",
    ]
    assert writer["when"] == (
        "not (nas_omv_mountpoint_audit[item.role].exists | bool)"
    )
    assert writer["changed_when"] is True
    assert writer["no_log"] is True
    assert "entry | to_json" in writer["ansible.builtin.command"]["argv"][-1]
    assert "'uuid'" not in writer["vars"]["entry"]
    assert "physical_disk_intent" not in writer["vars"]["entry"]
    for task in command_tasks:
        argv = task["ansible.builtin.command"]["argv"]
        assert argv[0] == "omv-confdbadm"
        assert argv[1] in {"read", "update"}
        if argv[1] == "read":
            assert task["changed_when"] is False
    for task in role:
        assert not any(name in task for name in [
            "ansible.builtin.shell", "ansible.builtin.raw",
            "ansible.builtin.script", "ansible.builtin.mount",
            "ansible.builtin.file", "ansible.builtin.copy",
        ])
    assert "nas_omv_registered.values() | list | unique | length == 3" in source
    # Before controlled cutover, no runnable playbook or role calls this
    # write-capable Ansible task file, even as an indirect include.
    ansible = ROOT / "automation/ansible"
    for folder in (ansible / "playbooks", ansible / "roles"):
        for path in folder.rglob("*.yml"):
            if path.name == "prepare-omv-register.yml":
                continue
            doc = path.read_text(encoding="utf-8")
            assert "tasks_from: prepare-omv-register.yml" not in doc, path
            assert "include_tasks: prepare-omv-register.yml" not in doc, path


def test_omv_post_registration_verification_is_read_only() -> None:
    path = ROOT / "automation/ansible/roles/nas/tasks/prepare-omv-verify.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and len(tasks) >= 7
    commands = []
    for task in tasks:
        modules = [k for k in task if k.startswith("ansible.builtin.")]
        assert len(modules) == 1, task.get("name")
        assert modules[0] in {
            "ansible.builtin.assert", "ansible.builtin.set_fact",
            "ansible.builtin.command",
        }, task.get("name")
        if "ansible.builtin.command" in task:
            assert task.get("changed_when") is False
            argv = task["ansible.builtin.command"]["argv"]
            assert argv[0] in {"findmnt", "zpool"}
            if argv[0] == "zpool":
                assert argv[1:3] == ["get", "-H"]
            commands.append(argv[0])
    assert set(commands) == {"findmnt", "zpool"}
    source = path.read_text(encoding="utf-8")
    assert "nas_omv_registered.keys() | list | sort" in source
    assert "nas_omv_verified_mounts" in source
    assert "nas_omv_verified_guids" in source
    assert "nas_omv_verified_volumes" in source
    assert "physical_recovery_journal.fingerprint" in source
    for folder in (
        ROOT / "automation/ansible/playbooks",
        ROOT / "automation/ansible/roles",
    ):
        for candidate in folder.rglob("*.yml"):
            if candidate == path:
                continue
            doc = candidate.read_text(encoding="utf-8")
            if candidate.name == "prepare-omv-activate.yml":
                assert "include_tasks: prepare-omv-verify.yml" in doc
                continue
            assert "tasks_from: prepare-omv-verify.yml" not in doc, candidate
            assert "include_tasks: prepare-omv-verify.yml" not in doc, candidate


def test_omv_fstab_stage_guards_existing_data() -> None:
    """A future OMV activation must not mount over unknown filesystem data."""
    path = ROOT / "automation/ansible/roles/nas/tasks/prepare-omv-activate.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and len(tasks) >= 10
    source = path.read_text(encoding="utf-8")
    assert "nas_omv_registration_authorized" in source
    assert "physical_recovery_journal.fingerprint" in source
    assert "nas_omv_activate_ext4_directory" in source
    assert "nas_omv_activate_ext4_contents" in source
    assert "nas_omv_activate_declared_ext4" in source
    assert "nas_omv_expected_mounts[0].fsname" in source
    assert "prepare-omv-verify.yml" in source
    for item in tasks:
        assert not any(key in item for key in (
            "ansible.builtin.shell", "ansible.builtin.raw",
            "ansible.builtin.script", "ansible.builtin.copy",
            "ansible.builtin.mount", "ansible.builtin.file",
        )), item.get("name")
        if "ansible.builtin.command" in item:
            argv = item["ansible.builtin.command"]["argv"]
            assert argv[0] in {"findmnt", "omv-salt", "mount"}
            if argv[0] == "omv-salt":
                assert argv == ["omv-salt", "deploy", "run", "fstab"]
                assert item["no_log"] is True
                assert item["when"]
            if argv[0] == "mount":
                assert item["when"] == "nas_omv_activate_ext4_after.rc == 1"
            assert "changed_when" in item
    for candidate in (ROOT / "automation/ansible/playbooks").glob("*.yml"):
        assert "prepare-omv-activate.yml" not in candidate.read_text(), candidate


def test_observed_identity_includes_system_disk_without_writes() -> None:
    """Controller state is derived from live hardware, never from guessed /dev/sdX."""
    path = ROOT / "automation/ansible/roles/nas/tasks/prepare-observed-identity.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and len(tasks) >= 7
    source = path.read_text(encoding="utf-8")
    assert "physical_recovery_disk_tree.stdout" in source
    assert "physical_observed_system_serial" in source
    assert "selectattr('serial', 'equalto', observed_serial)" in source
    assert "physical_observed_devices" in source
    assert "physical_observed_storage" in source
    assert "plan_fingerprint" in source
    assert "nas_omv_verified_volumes" in source
    for task in tasks:
        assert not any(key in task for key in (
            "ansible.builtin.shell", "ansible.builtin.raw",
            "ansible.builtin.script", "ansible.builtin.copy",
            "ansible.builtin.file", "ansible.builtin.mount",
        )), task.get("name")
        if "ansible.builtin.command" in task:
            argv = task["ansible.builtin.command"]["argv"]
            assert argv[:4] == ["jq", "-e", "-r", argv[3]]
            assert task.get("changed_when") is False
    for candidate in (ROOT / "automation/ansible/playbooks").glob("*.yml"):
        assert "prepare-observed-identity.yml" not in candidate.read_text(), candidate


def test_generic_observed_state_persistence_is_one_shot() -> None:
    """No state overwrite, no symlinks, no disk writes and no NAS code in 910."""
    path = ROLE / "persist-verified.yml"
    tasks = read("persist-verified.yml")
    source = path.read_text(encoding="utf-8")
    assert "physical_storage_state_commit_authorized" in source
    assert "physical_state_ssh_identity.fingerprint" in source
    assert "physical_state_ssh_identity.address == ansible_host" in source
    assert "physical_observed_storage.plan_fingerprint == physical_disk_intent.fingerprint" in source
    assert "physical_state_legacy_file" in source
    assert "physical_state_existing_bytes.content | b64decode | from_json" in source
    assert "physical_storage_state_document" in source
    assert "physical_state_verified_bytes.content | b64decode | from_json" in source
    copies = [t for t in tasks if "ansible.builtin.copy" in t]
    assert len(copies) == 1
    writer = copies[0]
    payload = writer["ansible.builtin.copy"]
    assert payload["force"] is False
    assert payload["follow"] is False
    assert payload["unsafe_writes"] is False
    assert payload["mode"] == "0600"
    assert writer["delegate_to"] == "localhost"
    assert writer["become"] is False
    assert writer["no_log"] is True
    assert writer["when"] == (
        "not (physical_state_existing_file.stat.exists | default(false))"
    )
    assert payload["dest"].endswith("-storage.json")
    assert "physical_storage_state_document | to_nice_json" in payload["content"]
    for task in tasks:
        modules = [k for k in task if k.startswith("ansible.builtin.")]
        assert len(modules) == 1
        assert modules[0] in {
            "ansible.builtin.assert", "ansible.builtin.stat",
            "ansible.builtin.slurp", "ansible.builtin.set_fact",
            "ansible.builtin.copy",
        }, task.get("name")
    for candidate in (ROOT / "automation/ansible/playbooks").glob("*.yml"):
        assert "persist-verified.yml" not in candidate.read_text(), candidate
    for candidate in (ROOT / "automation/ansible/roles").rglob("*.yml"):
        if candidate == path:
            continue
        assert "tasks_from: persist-verified.yml" not in candidate.read_text(), candidate
        assert "include_tasks: persist-verified.yml" not in candidate.read_text(), candidate


def read_omv_register() -> list:
    name = ROOT / "automation/ansible/roles/nas/tasks/prepare-omv-register.yml"
    content = yaml.safe_load(name.read_text(encoding="utf-8"))
    assert isinstance(content, list)
    return content


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
    test_omv_native_registration_is_safe_and_disconnected()
    test_omv_post_registration_verification_is_read_only()
    test_omv_fstab_stage_guards_existing_data()
    test_observed_identity_includes_system_disk_without_writes()
    test_generic_observed_state_persistence_is_one_shot()
    print("[ОК] Ansible one-shot journal and isolated first-create stage")
