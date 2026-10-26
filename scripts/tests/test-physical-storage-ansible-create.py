#!/usr/bin/env python3
"""Contracts for staged native Ansible disk creation without production cutover."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "automation/ansible/roles/physical_storage/tasks"


def read(name: str):
    result = yaml.safe_load((ROLE / name).read_text(encoding="utf-8"))
    assert isinstance(result, list) and result
    return result


def test_recreate_preflight_is_strictly_non_destructive() -> None:
    """Reinitialize intent never means that a mount or system disk is safe."""
    tasks = read("prepare-recreate-preflight.yml")
    commands = []
    for task in tasks:
        modules = [k for k in task if k.startswith("ansible.builtin.")]
        assert len(modules) == 1, task.get("name")
        assert modules[0] in {
            "ansible.builtin.assert", "ansible.builtin.set_fact",
            "ansible.builtin.stat", "ansible.builtin.find",
            "ansible.builtin.command", "ansible.builtin.include_role",
            "ansible.builtin.debug",
        }
        if modules[0] == "ansible.builtin.command":
            assert task.get("changed_when") is False, task["name"]
            cmd = task["ansible.builtin.command"]["argv"]
            assert cmd[0] in {"lsblk", "jq", "readlink"}, cmd
            commands.append(cmd[0])
        if modules[0] == "ansible.builtin.include_role":
            assert task[modules[0]] == {
                "name": "physical_storage", "tasks_from": "inspect.yml",
            }
    assert set(commands) == {"lsblk", "jq", "readlink"}
    source = (ROLE / "prepare-recreate-preflight.yml").read_text()
    assert "physical_storage.initialization.recreate_once" in source
    assert "physical_disk_recreate_authorized" in source
    assert "physical_disk_intent.fingerprint" in source
    assert "physical_recreate_previous_state" in source
    assert "physical_recreate_previous_journal" in source
    assert "physical_recreate_by_id" in source
    assert "physical_recreate_actual" in source
    assert "mountpoints" in source
    assert "select(any(.. | objects | .mountpoints? | arrays | .[]; . == \"/\"))" in source
    # No production playbook includes this role until the rest of the
    # interruption-safe state machine has been verified.
    for playbook in (ROOT / "automation/ansible/playbooks").rglob("*.yml"):
        assert "prepare-recreate-preflight.yml" not in playbook.read_text(), playbook


def test_recreate_refuses_nested_system_root() -> None:
    """The selected serial must not own /, even when root is nested in LVM."""
    tasks = read("prepare-recreate-preflight.yml")
    checks = [
        task["ansible.builtin.command"]["argv"][-1]
        for task in tasks
        if task.get("ansible.builtin.command", {}).get("argv", [""])[0] == "jq"
        and "--arg" in task["ansible.builtin.command"]["argv"]
    ]
    assert len(checks) == 1
    disk_tree = json.dumps({"blockdevices": [
        {"type": "disk", "serial": "SYSTEM", "mountpoints": [None], "children": [
            {"type": "part", "mountpoints": [None], "children": [
                {"type": "lvm", "mountpoints": ["/"]}
            ]}
        ]},
        {"type": "disk", "serial": "SHARED", "mountpoints": [None]},
        {"type": "disk", "serial": "DATA", "mountpoints": ["/nas-data"]},
    ]})
    def check(serial: str) -> int:
        result = subprocess.run(
            ["jq", "-e", "--arg", "serial", serial, checks[0]],
            input=disk_tree, text=True, capture_output=True, check=False,
        )
        return result.returncode
    assert check("SYSTEM") != 0, "The root disk was approved for recreation"
    assert check("SHARED") == 0
    assert check("DATA") == 0


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
        if p.name == "prepare-create-complete.yml":
            assert "tasks_from: prepare-write.yml" in source
            assert "tasks_from: prepare-journal.yml" in source
        else:
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
    assert "physical_recovery_journal.fingerprint == physical_recovery_intent.fingerprint" in str(tasks)
    assert "physical_recovery_journal['items'] == physical_recovery_intent['items']" in str(tasks)
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
    assert "physical_recovery_journal.fingerprint == physical_recovery_intent.fingerprint" in str(audit)
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
    assert "physical_recovery_journal['items'] == physical_recovery_intent['items']" in source
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
            if path.name == "prepare-recover-complete.yml":
                assert "include_tasks: prepare-omv-register.yml" in doc
            else:
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
    """No state overwrite, only trusted 910 symlink, no NAS code in 910."""
    path = ROLE / "persist-verified.yml"
    tasks = read("persist-verified.yml")
    source = path.read_text(encoding="utf-8")
    assert "physical_storage_state_commit_authorized" in source
    assert "physical_state_ssh_identity.fingerprint" in source
    assert "physical_state_ssh_identity.address == ansible_host" in source
    assert "physical_observed_storage.plan_fingerprint == physical_recovery_intent.fingerprint" in source
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
            "ansible.builtin.copy", "ansible.builtin.command",
        }, task.get("name")
        if modules[0] == "ansible.builtin.command":
            assert task["ansible.builtin.command"]["argv"] == [
                "readlink", "--canonicalize-existing",
                "/var/lib/infra-manager/physical-hosts",
            ]
            assert task["changed_when"] is False
            assert task["delegate_to"] == "localhost"
    for candidate in (ROOT / "automation/ansible/playbooks").glob("*.yml"):
        assert "persist-verified.yml" not in candidate.read_text(), candidate
    for candidate in (ROOT / "automation/ansible/roles").rglob("*.yml"):
        if candidate == path:
            continue
        source = candidate.read_text()
        if candidate.name == "prepare-recover-complete.yml":
            assert "tasks_from: persist-verified.yml" in source
        else:
            assert "tasks_from: persist-verified.yml" not in source, candidate
        assert "include_tasks: persist-verified.yml" not in source, candidate
    assert "'/mnt/persistent-state/physical-hosts'" in source_of_role(path)
    assert "physical_state_real_directory.stat.mode in ['0700', '0750']" in source_of_role(path)


def source_of_role(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_disabled_recovery_transaction_has_strict_order() -> None:
    """Staged coordinator must be unreachable and check hardware first."""
    path = ROOT / "automation/ansible/roles/nas/tasks/prepare-recover-complete.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and len(tasks) == 8
    gate = tasks[0]["ansible.builtin.assert"]["that"]
    assert any("nas_omv_registration_authorized" in item for item in gate)
    assert any("physical_storage_state_commit_authorized" in item for item in gate)
    assert any("physical_recovery_intent.mode == 'recover'" in item for item in gate)
    assert any("physical_recovery_intent.fingerprint == physical_disk_intent.fingerprint" in item for item in gate)
    sequence = []
    for task in tasks[1:-1]:
        if "ansible.builtin.include_role" in task:
            arg = task["ansible.builtin.include_role"]
            sequence.append((arg["name"], arg["tasks_from"]))
        else:
            sequence.append(("nas", task["ansible.builtin.include_tasks"]))
    assert sequence == [
        ("physical_storage", "prepare-recover-inspect.yml"),
        ("nas", "prepare-omv-audit.yml"),
        ("nas", "prepare-omv-register.yml"),
        ("nas", "prepare-omv-activate.yml"),
        ("nas", "prepare-observed-identity.yml"),
        ("physical_storage", "persist-verified.yml"),
    ]
    assert "ansible.builtin.debug" in tasks[-1]
    for playbook in (ROOT / "automation/ansible/playbooks").rglob("*.yml"):
        assert "prepare-recover-complete.yml" not in playbook.read_text(), playbook
    for role in (ROOT / "automation/ansible/roles").rglob("*.yml"):
        if role == path:
            continue
        source = role.read_text()
        if role.name == "prepare-create-complete.yml":
            assert "include_tasks: prepare-recover-complete.yml" in source
        else:
            assert "prepare-recover-complete.yml" not in source, role



def test_disabled_first_create_is_journal_first_and_unreachable() -> None:
    """Never retry mkfs: journal must precede the one-shot disk writer."""
    path = ROOT / "automation/ansible/roles/nas/tasks/prepare-create-complete.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert len(tasks) == 7
    guard = tasks[0]["ansible.builtin.assert"]["that"]
    assert any("nas_first_create_authorized" in item for item in guard)
    assert any("nas_omv_registration_authorized" in item for item in guard)
    assert any("physical_storage_state_commit_authorized" in item for item in guard)
    assert any("physical_disk_intent.mode == 'first-create'" in item for item in guard)
    assert any("physical_disk_intent.fingerprint" in item for item in guard)
    steps = [
        step["ansible.builtin.include_role"]["tasks_from"]
        if "ansible.builtin.include_role" in step
        else step.get("ansible.builtin.include_tasks")
        for step in tasks[1:-1]
        if "ansible.builtin.set_fact" not in step
    ]
    assert steps == [
        "prepare-preflight.yml",
        "prepare-journal.yml",
        "prepare-write.yml",
        "prepare-recover-complete.yml",
    ]
    assert tasks[4]["ansible.builtin.set_fact"]["physical_recovery_intent"] == (
        "{{ physical_disk_intent | combine({'mode': 'recover'}) }}"
    )
    assert "ansible.builtin.debug" in tasks[-1]
    # Recovery must read a separate fact: -e physical_disk_intent has
    # highest precedence and cannot be rewritten via set_fact.
    for name in (
        "inspect-nas-storage-recovery.yml",
        "inspect-physical-storage-recovery.yml",
    ):
        entry = yaml.safe_load(
            (ROOT / "automation/ansible/playbooks" / name).read_text()
        )
        assert entry[0]["vars"]["physical_recovery_intent"] == (
            "{{ physical_disk_intent }}"
        )
    recovery = yaml.safe_load(
        (ROOT / "automation/ansible/roles/nas/tasks/prepare-recover-complete.yml").read_text()
    )
    assert "physical_recovery_intent is mapping" in recovery[0]["ansible.builtin.assert"]["that"]
    assert "physical_recovery_intent.fingerprint == physical_disk_intent.fingerprint" in (
        recovery[0]["ansible.builtin.assert"]["that"]
    )
    for playbook in (ROOT / "automation/ansible/playbooks").rglob("*.yml"):
        assert "prepare-create-complete.yml" not in playbook.read_text(), playbook
    for role in (ROOT / "automation/ansible/roles").rglob("*.yml"):
        if role == path:
            continue
        assert "prepare-create-complete.yml" not in role.read_text(), role


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
    test_recreate_preflight_is_strictly_non_destructive()
    test_recreate_refuses_nested_system_root()
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
    test_disabled_recovery_transaction_has_strict_order()
    test_disabled_first_create_is_journal_first_and_unreachable()
    print("[ОК] Ansible one-shot journal and isolated first-create stage")
