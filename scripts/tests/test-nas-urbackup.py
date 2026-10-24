#!/usr/bin/env python3
"""Оффлайн-проверки UrBackup NAS без Docker, дисков, SSH или паролей."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import tarfile
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_nas_backup_policy import (
    load_nas_backup_policy, verify_nas_backup_volumes,
)


def module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "automation/ansible/files" / path
    )
    assert spec and spec.loader, path
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


guard = module("nas-urbackup-safe.py", "nas_urbackup_guard_test")
snapshot = module("nas-urbackup-snapshot.py", "nas_urbackup_snapshot_test")
recovery = module("nas-urbackup-recovery.py", "nas_urbackup_recovery_test")


def refuses(cb, cls=Exception):
    try:
        cb()
    except cls:
        return
    raise AssertionError("Опасная операция была разрешена")


def test_backup_policy():
    policy = load_nas_backup_policy(ROOT)
    assert policy["engine"] == "urbackup"
    assert policy["local_client"]["source_volume"] == "data"
    assert policy["local_client"]["target_volume"] == "backup"
    assert policy["retention"]["keep_daily"] >= 7
    assert policy["retention"]["keep_weekly"] >= 4
    assert policy["server"]["web_bind"] == "127.0.0.1"

    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "infrastructure/hosts/nas"
        fake.mkdir(parents=True)
        schema = Path(td) / "infrastructure/schemas"
        schema.mkdir(parents=True)
        (schema / "nas-backup.schema.yaml").write_text(
            (ROOT / "infrastructure/schemas/nas-backup.schema.yaml")
            .read_text(encoding="utf-8"), encoding="utf-8"
        )
        def write_p(document):
            (fake / "backup.yaml").write_text(
                yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
            )

        broken = copy.deepcopy(policy)
        broken["server"]["web_bind"] = "0.0.0.0"
        write_p(broken)
        refuses(lambda: load_nas_backup_policy(Path(td)), InfraManagerError)

        broken = copy.deepcopy(policy)
        broken["server"]["image"] = "uroni/urbackup-server:latest"
        write_p(broken)
        refuses(lambda: load_nas_backup_policy(Path(td)), InfraManagerError)

        broken = copy.deepcopy(policy)
        broken["local_client"]["target_volume"] = "data"
        write_p(broken)
        refuses(lambda: load_nas_backup_policy(Path(td)), InfraManagerError)

        broken = copy.deepcopy(policy)
        broken["retention"]["keep_daily"] = 0
        write_p(broken)
        refuses(lambda: load_nas_backup_policy(Path(td)), InfraManagerError)


def test_volume_contract():
    policy = load_nas_backup_policy(ROOT)
    v = [
        SimpleNamespace(role="shared", source="/dev/sdb", mountpoint="/srv/ext4", filesystem="ext4"),
        SimpleNamespace(role="data", source="nas-data", mountpoint="/nas-data", filesystem="zfs"),
        SimpleNamespace(role="backup", source="nas-backup", mountpoint="/nas-backup", filesystem="zfs"),
    ]
    assert verify_nas_backup_volumes(policy, tuple(v)) == ("/nas-data", "/nas-backup")
    v[2] = SimpleNamespace(role="backup", source="nas-data", mountpoint="/nas-backup", filesystem="zfs")
    refuses(lambda: verify_nas_backup_volumes(policy, tuple(v)), InfraManagerError)


def test_live_guard_is_read_only():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "data"
        dest = Path(td) / "backup"
        src.mkdir()
        dest.mkdir()
        cfg = Path(td) / "config.json"
        good = {
            "source_pool": "nas-data", "source_guid": "123",
            "source_mount": str(src),
            "backup_pool": "nas-backup", "backup_guid": "456",
            "backup_mount": str(dest),
        }
        cfg.write_text(json.dumps(good), encoding="utf-8")
        def fake_call(*args):
            if args[:3] == ("zpool", "get", "-H"):
                return good["source_guid"] if args[-1] == "nas-data" else good["backup_guid"]
            if args[:3] == ("zpool", "list", "-H"):
                return "ONLINE"
            if args[0] == "findmnt":
                role = "data" if args[3] == str(src) else "backup"
                return ("nas-" + role) if args[-1] == "SOURCE" else "zfs"
            raise AssertionError(args)

        with patch.object(guard, "CONFIG", cfg), patch.object(guard, "call", side_effect=fake_call):
            assert guard.verify() == good
            damaged = {**good, "backup_guid": "123"}
            cfg.write_text(json.dumps(damaged), encoding="utf-8")
            refuses(guard.verify, guard.SafetyError)
            damaged = {**good, "backup_mount": str(src)}
            cfg.write_text(json.dumps(damaged), encoding="utf-8")
            refuses(guard.verify, guard.SafetyError)


def test_scripts_have_no_destructive_fallback():
    for file in ("nas-urbackup-safe.py", "nas-urbackup-snapshot.py",
                 "nas-urbackup-recovery.py"):
        text = (ROOT / "automation/ansible/files" / file).read_text(encoding="utf-8")
        for unsafe in ("mkfs", "wipefs", "zpool create", "zpool destroy", "zfs rollback",
                       "docker volume prune"):
            assert unsafe not in text, (file, unsafe)
    assert snapshot.STAGING.as_posix().startswith("/run/")
    assert "recovery-key" not in (ROOT / "automation/ansible/files/nas-urbackup-recovery.py").read_text(encoding="utf-8")
    assert "AES" not in (ROOT / "automation/ansible/files/nas-urbackup-recovery.py").read_text(encoding="utf-8")
    assert "confirmed" in (
        ROOT / "automation/ansible/files/nas-urbackup-snapshot.py"
    ).read_text(encoding="utf-8")



def test_plain_state_archive_verification():
    """Архив читается без ключа; повреждённые данные отвергаются SHA-256."""
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        archive = folder / "state-20261024T120000Z.tar"
        with tarfile.open(archive, "w") as tar:
            state_dir = tarfile.TarInfo(recovery.PATHS[0])
            state_dir.type = tarfile.DIRTYPE
            tar.addfile(state_dir)
            for filename in recovery.PATHS[1:]:
                data = b"test-only-no-secret"
                record = tarfile.TarInfo(filename)
                record.size = len(data)
                tar.addfile(record, io.BytesIO(data))
        checksum = recovery.digest(archive)
        archive.with_suffix(".sha256").write_text(checksum + "\n", encoding="ascii")
        with patch.object(recovery, "destination", return_value=folder):
            recovery.verify()
            assert archive.open("rb").read(6) != b"Salted"
            with archive.open("ab") as data:
                data.write(b"modified")
            refuses(recovery.verify, recovery.RecoveryError)
        assert "recovery-key" not in recovery.PATHS



def test_docker_and_deploy_contract():
    folder = ROOT / "automation/ansible"
    compose = (folder / "templates/nas-urbackup.compose.yml.j2").read_text()
    assert "create_host_path: false" in compose
    assert '127.0.0.1:55414' in compose
    assert '127.0.0.1:55413:55413/tcp' in compose
    assert '"55413:55413/tcp"' not in compose
    assert 'restart: "no"' in compose
    assert "/var/urbackup" in compose
    unit = (folder / "templates/nas-urbackup.service.j2").read_text()
    assert "ExecStartPre=" in unit and "nas-urbackup-safe preflight" in unit
    assert "RequiresMountsFor=" in unit
    operations = (
        ROOT / "scripts/infra-manager/infra_manager/physical_host_operations.py"
    ).read_text()
    assert "configure_nas_urbackup_local" in operations
    assert "configure_nas_backups(" not in operations
    assert "load_nas_backup_policy" in operations
    playbook = (folder / "playbooks/configure-nas-urbackup.yml").read_text()
    assert 'group: nas-urbackup' in playbook
    assert 'mode: "0710"' in playbook
    safe = (folder / "files/nas-urbackup-safe.py").read_text()
    assert "if PASSWORD.exists():" in safe
    assert "O_EXCL" in safe
    for name in ("configure-nas-urbackup.yml", "configure-nas-urbackup-client.yml"):
        assert isinstance(yaml.safe_load((folder / "playbooks" / name).read_text()), list)


def main() -> int:
    test_backup_policy()
    test_volume_contract()
    test_live_guard_is_read_only()
    test_scripts_have_no_destructive_fallback()
    test_plain_state_archive_verification()
    test_docker_and_deploy_contract()
    print("[ОК] NAS UrBackup: политика, GUID, безопасность и новые задания")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
