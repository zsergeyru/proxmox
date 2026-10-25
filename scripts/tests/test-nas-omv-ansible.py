#!/usr/bin/env python3
"""Offline safety contract for OMV confdb reconciliation using Ansible, no NAS access."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
tasks_dir = ROOT / "automation/ansible/roles/nas/tasks"
smb = yaml.safe_load((tasks_dir / "smb.yml").read_text(encoding="utf-8"))
omv = yaml.safe_load((tasks_dir / "omv-shares.yml").read_text(encoding="utf-8"))

assert isinstance(smb, list) and isinstance(omv, list)
assert any(t.get("ansible.builtin.include_tasks") == "omv-shares.yml" for t in smb)
assert not any("nas-omv-shares" in str(t) for t in smb), "Python OMV helper still active"

operations = []
for task in omv:
    # The reconciliation must use built-in Ansible modules, not uploaded scripts.
    assert not any(key in task for key in (
        "ansible.builtin.script", "ansible.builtin.shell", "ansible.builtin.raw",
        "ansible.builtin.copy", "ansible.builtin.template",
    )), task["name"]
    if "ansible.builtin.command" in task:
        argv = task["ansible.builtin.command"]["argv"]
        assert argv[0] in ("omv-confdbadm", "omv-salt"), task["name"]
        assert argv[1] in ("read", "update", "deploy"), task["name"]
        if argv[1] == "update":
            assert argv[2] in (
                "conf.system.sharedfolder",
                "conf.service.smb.share",
                "conf.service.smb",
            ), task["name"]
            assert task.get("no_log") is True, task["name"]
        operations.append((task["name"], argv))
assert len(operations) >= 7

names = [x["name"] for x in omv]
def pos(needle):
    return next(i for i, name in enumerate(names) if needle in name)

assert pos("Прочитать существующие папки") < pos("Отвергнуть чужие")
assert pos("Отвергнуть чужие") < pos("Создать только отсутствующие")
assert pos("Создать только отсутствующие") < pos("Прочитать действительные UUID")
assert pos("Прочитать действительные UUID") < pos("Проверить отсутствие конкурирующих")
assert pos("Проверить отсутствие конкурирующих") < pos("Включить Samba")
assert pos("Включить Samba") < pos("Создать отсутствующие SMB")
assert pos("Создать отсутствующие SMB") < pos("Применить конфигурацию Samba")

source = (tasks_dir / "omv-shares.yml").read_text(encoding="utf-8")
for guard in (
    "existing_named | length <= 1",
    "existing_path | length == 0",
    "existing_smb | length <= 1",
    "existing_named[0].mntentref == target_ref",
    "existing_named[0].uuid",
    "nas_omv_folders_after.stdout | from_json",
    "nas_provision.shares",
    "'enable': true, 'guest': 'no', 'readonly': false",
):
    assert guard in source, guard
for forbidden in ("mkfs", "wipefs", "sgdisk", "zpool create", "zpool destroy",
                  "shutil", "/etc/samba/smb.conf", "password:"):
    assert forbidden not in source, forbidden
print("[ОК] OMV: Ansible-only, UUID от confdb, защита чужих ресурсов")
