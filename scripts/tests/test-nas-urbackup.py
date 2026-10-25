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
        name, ROOT / "automation/ansible/roles/nas/files" / path
    )
    assert spec and spec.loader, path
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


guard = module("nas-urbackup-safe.py", "nas_urbackup_guard_test")
snapshot = module("nas-urbackup-snapshot.py", "nas_urbackup_snapshot_test")
recovery = module("nas-urbackup-recovery.py", "nas_urbackup_recovery_test")
retention = module("nas-urbackup-policy.py", "nas_urbackup_policy_test")


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
    expected = {"data": "nas-data", "backup": "nas-backup"}
    assert verify_nas_backup_volumes(
        policy, tuple(v), expected_pools=expected,
    ) == ("/nas-data", "/nas-backup")
    v[2] = SimpleNamespace(role="backup", source="nas-data", mountpoint="/nas-backup", filesystem="zfs")
    refuses(lambda: verify_nas_backup_volumes(
        policy, tuple(v), expected_pools=expected,
    ), InfraManagerError)
    v[2] = SimpleNamespace(role="backup", source="nas-backup", mountpoint="/nas-backup", filesystem="zfs")
    refuses(lambda: verify_nas_backup_volumes(
        policy, tuple(v), expected_pools={"data": "nas-backup", "backup": "nas-data"},
    ), InfraManagerError)


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
            "backup_subdir": "urbackup/backups",
            "client_container": "nas-urbackup-client",
            "client_name": "nas-local",
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
        text = (ROOT / "automation/ansible/roles/nas/files" / file).read_text(encoding="utf-8")
        for unsafe in ("mkfs", "wipefs", "zpool create", "zpool destroy", "zfs rollback",
                       "docker volume prune"):
            assert unsafe not in text, (file, unsafe)
    assert snapshot.STAGING.as_posix().startswith("/run/")
    assert "recovery-key" not in (ROOT / "automation/ansible/roles/nas/files/nas-urbackup-recovery.py").read_text(encoding="utf-8")
    assert "AES" not in (ROOT / "automation/ansible/roles/nas/files/nas-urbackup-recovery.py").read_text(encoding="utf-8")
    assert "confirmed" in (
        ROOT / "automation/ansible/roles/nas/files/nas-urbackup-snapshot.py"
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
    compose = (folder / "roles/nas/templates/nas-urbackup.compose.yml.j2").read_text()
    assert "create_host_path: false" in compose
    assert "nas_backup_policy.server.web_bind" in compose
    assert "nas_backup_policy.server.web_port" in compose
    assert "nas_backup_policy.server.name" in compose
    assert '127.0.0.1:55413:55413/tcp' in compose
    assert '"55413:55413/tcp"' not in compose
    assert 'restart: "no"' in compose
    assert "/var/urbackup" in compose
    unit = (folder / "roles/nas/templates/nas-urbackup.service.j2").read_text()
    assert "ExecStartPre=" in unit and "nas-urbackup-safe preflight" in unit
    assert "RequiresMountsFor=" in unit
    operations = (
        ROOT / "scripts/infra-manager/drivers/omv8.py"
    ).read_text()
    assert "run_nas_phase(" in operations
    assert "configure_nas_backups(" not in operations
    assert "load_nas_backup_policy" in operations
    playbook = (folder / "roles/nas/tasks/urbackup_server.yml").read_text()
    assert 'group: nas-urbackup' in playbook
    assert 'mode: "0710"' in playbook
    safe = (folder / "roles/nas/files/nas-urbackup-safe.py").read_text()
    assert "if PASSWORD.exists():" in safe
    assert "O_EXCL" in safe
    for name in ("urbackup_server.yml", "urbackup_client.yml"):
        tasks = yaml.safe_load((folder / "roles/nas/tasks" / name).read_text())
        assert isinstance(tasks, list)
        for task in tasks:
                args = task.get("ansible.builtin.command")
                if args is not None:
                    # YAML принимает список как значение модуля, но Ansible
                    # выдаёт "unexpected parameter type in action".
                    assert isinstance(args, (dict, str)), (name, task["name"])
                    if isinstance(args, dict):
                        assert isinstance(args.get("argv"), list), (name, task["name"])
                        assert all(isinstance(item, str) for item in args["argv"])




def test_permanent_docker_client_contract():
    """Ни скачивания хостового клиента, ни проброса физических дисков."""
    folder = ROOT / "automation/ansible"
    play = yaml.safe_load(
        (folder / "roles/nas/tasks/urbackup_client.yml").read_text()
    )
    steps = play
    descriptions = " ".join(task["name"] for task in steps)
    assert "Скачать" not in descriptions
    assert "установщик" not in descriptions.lower()
    assert all("ansible.builtin.get_url" not in task for task in steps)
    assert "nas-urbackup-client.service" in str(steps)
    assert "urbackupclientbackend.service" in str(steps)  # disable legacy only
    server_compose = (folder / "roles/nas/templates/nas-urbackup.compose.yml.j2").read_text()
    client_compose = (
        folder / "roles/nas/templates/nas-urbackup-client.compose.yml.j2"
    ).read_text()
    assert "nas-urbackup-lan" in server_compose
    assert "nas_backup_policy.local_client.container" in client_compose
    assert "nas-urbackup-lan" in client_compose
    assert "external: true" in client_compose
    assert 'command: ["--config", "/etc/default/urbackupclient"]' in client_compose
    assert "source: /etc/nas-apps/urbackup/client-defaults" in client_compose
    client_defaults = (
        folder / "roles/nas/templates/nas-urbackup-client.defaults.j2"
    ).read_text()
    assert "INTERNET_ONLY=false" in client_defaults
    assert "RESTORE=disabled" in client_defaults
    assert "name: nas-urbackup-client" in client_compose
    assert "uroni/urbackup-client" not in server_compose
    assert 'entrypoint: ["/usr/local/sbin/urbackupclientbackend"]' in client_compose
    assert "URBACKUP_CLIENT_AUTHKEY" not in client_compose
    assert "URBACKUP_BACKUP_VOLUMES" not in client_compose
    assert "no-new-privileges:true" in client_compose
    assert "propagation: rslave" in client_compose
    assert "read_only: true" in client_compose
    assert "/run/nas-urbackup-source" in client_compose
    assert "/nas-backup" not in client_compose
    assert "/nas-data" not in client_compose
    assert "/dev/" not in client_compose
    assert "ports:" not in client_compose
    assert "create_host_path: false" in client_compose
    mount = module("nas-urbackup-client-mount.py", "nas_mount_policy_test")
    assert mount.STAGING.as_posix() == "/run/nas-urbackup-source"
    source = (
        folder / "roles/nas/files/nas-urbackup-client-mount.py"
    ).read_text(encoding="utf-8")
    assert "mount" in source and "--make-rshared" in source
    assert "--bind" in source
    snapshot_script = (folder / "roles/nas/files/nas-urbackup-snapshot.py").read_text()
    assert 'MOUNT = STAGING / "current"' in snapshot_script
    assert 'GUEST_SOURCE = "/backup-source/current"' in snapshot_script
    assert 'return ("docker", "exec", name, "urbackupclientctl")' in snapshot_script
    assert '"/usr/local/bin/urbackupclientctl"' not in snapshot_script
    assert "os.path.ismount(str(MOUNT))" in snapshot_script
    assert 'run(MOUNT_GUARD, "check")' in snapshot_script
    client_unit = (
        folder / "roles/nas/templates/nas-urbackup-client.service.j2"
    ).read_text()
    assert "RequiresMountsFor=" in client_unit
    assert "nas-urbackup-safe preflight" in client_unit
    assert "nas-urbackup-client-mount check" in client_unit
    assert "nas-urbackup.service" in client_unit
    assert "BindsTo=nas-urbackup.service" in client_unit
    assert "nas-urbackup-client.service" in (
        folder / "roles/nas/templates/nas-urbackup-local.service.j2"
    ).read_text()
    status = yaml.safe_load((ROOT / "infrastructure/hosts/nas/status.yaml").read_text())
    assert status["host"] == "nas"
    assert any(
        item.get("type") == "docker"
        and item.get("component") == "urbackup_client"
        and item.get("container_from") == "backup.local_client.container"
        for item in status["checks"]
    )
    assert any(
        x.get("container_from") == "backup.server.name"
        for x in status["checks"]
    )
    assert load_nas_backup_policy(ROOT)["local_client"]["container"] == "nas-urbackup-client"
    assert any(x.get("unit") == "nas-urbackup-client.service"
               for x in status["checks"])
    physical_status = (ROOT / "scripts/infra-manager/infra_manager/physical_nas_status.py").read_text()
    assert "load_declared_checks(" in physical_status
    assert "_run_declared_check(" in physical_status


def test_confirm_client_status_and_server_completed_pointer():
    """Client browse needs restoration tokens; server current is completion proof."""
    with patch.object(snapshot, "run", return_value=json.dumps({
        "servers": [{"name": "172.20.0.2", "internet_connection": False}],
        "internet_status": "connected_local",
        "last_backup_time": 0,
    })) as status:
        cfg = {"client_container": "nas-urbackup-client"}
        assert snapshot.client_status(cfg)["internet_status"] == "connected_local"
        status.assert_called_once_with(*snapshot.client_command(cfg), "status")
    with patch.object(snapshot, "run", return_value="No file backup access tokens found"):
        refuses(lambda: snapshot.client_status(cfg), snapshot.SnapshotError)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "nas-backup" / "urbackup" / "backups"
        root.mkdir(parents=True)
        config = {
            "backup_mount": str(Path(td) / "nas-backup"),
            "backup_subdir": "urbackup/backups",
            "client_name": "nas-local",
        }
        assert snapshot.server_current(config) is None
        client = root / "nas-local"
        client.mkdir()
        assert snapshot.server_current(config) is None

        first = client / "261024-0300"
        (first / "nas-data").mkdir(parents=True)
        (client / "current").symlink_to(first.name)
        old = snapshot.server_current(config)
        assert old == str(first)
        second = client / "261025-0300"
        (second / "nas-data").mkdir(parents=True)
        (client / "current").unlink()
        (client / "current").symlink_to(second.name)
        new = snapshot.server_current(config)
        assert new == str(second) and old != new

        # A new pointer to a completed but wrong/empty backup set is no proof.
        (second / "nas-data").rmdir()
        assert snapshot.server_current(config) is None

        # A current symlink must never escape the expected client directory.
        (client / "current").unlink()
        (client / "current").symlink_to(root)
        refuses(lambda: snapshot.server_current(config), snapshot.SnapshotError)
        (client / "current").unlink()

    source = (
        ROOT / "automation/ansible/roles/nas/files/nas-urbackup-snapshot.py"
    ).read_text()
    assert 'run(*client_command(config), "start", "-f", timeout=21600)' in source
    assert '"browse"' not in source
    assert "server_current(config)" in source
    assert "current != previous" in source
    assert "time.monotonic() + 1800" in source


def test_mount_propagation_requires_verified_state():
    mount = module("nas-urbackup-client-mount.py", "nas_mount_policy_again")
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "staging"
        source.mkdir()
        with patch.object(mount, "STAGING", source), patch.object(
            mount, "is_mountpoint", return_value=False
        ):
            refuses(mount.check, mount.MountError)

    assert "uroni/urbackup-client:2.5.31" == load_nas_backup_policy(
        ROOT
    )["local_client"]["image"]


def test_bind_mount_detection_uses_findmnt_not_python_ismount():
    """A bind mount on its own filesystem is not reliably detected by os.path."""
    mount = module("nas-urbackup-client-mount.py", "nas_bind_detection_test")
    with patch.object(mount.subprocess, "run", return_value=SimpleNamespace(
        returncode=0, stdout=str(mount.STAGING) + "\n"
    )) as subprocess_run, patch.object(
        mount.os.path, "ismount", side_effect=AssertionError("do not use ismount")
    ):
        assert mount.is_mountpoint() is True
        subprocess_run.assert_called_once_with(
            ("findmnt", "-n", "-M", str(mount.STAGING), "-o", "TARGET"),
            capture_output=True, text=True, timeout=30,
        )
    with patch.object(mount.subprocess, "run", return_value=SimpleNamespace(
        returncode=1, stdout=""
    )):
        assert mount.is_mountpoint() is False
    with patch.object(mount.subprocess, "run", return_value=SimpleNamespace(
        returncode=32, stdout=""
    )):
        refuses(mount.is_mountpoint, mount.MountError)


def test_staging_mount_is_idempotent():
    """Existing same-filesystem bind must not produce another nested mount."""
    mount = module("nas-urbackup-client-mount.py", "nas_idempotent_mount_test")
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "staging"
        commands = []
        def fake_run(*args):
            commands.append(args)
            if args[0] == "findmnt":
                return "shared"
            return ""
        with patch.object(mount, "STAGING", source), patch.object(
            mount, "run", side_effect=fake_run
        ), patch.object(
            mount, "is_mountpoint", side_effect=[False, True, True, True]
        ):
            mount.prepare()
            mount.prepare()
        bind_commands = [args for args in commands if args[:2] == ("mount", "--bind")]
        assert bind_commands == [("mount", "--bind", str(source), str(source))]
        assert commands.count(("mount", "--make-rshared", str(source))) == 2

    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "staging"
        source.mkdir()
        (source / "unexpected.txt").write_text("must be preserved")
        with patch.object(mount, "STAGING", source), patch.object(
            mount, "run", return_value=""
        ), patch.object(mount, "is_mountpoint", return_value=False):
            refuses(mount.prepare, mount.MountError)
        assert (source / "unexpected.txt").read_text() == "must be preserved"


def test_mount_failure_output_has_no_arbitrary_stderr():
    mount = module("nas-urbackup-client-mount.py", "nas_mount_error_test")
    with patch.object(mount.subprocess, "run", return_value=SimpleNamespace(
        returncode=32, stdout="password=TOP_SECRET", stderr="token=TOP_SECRET"
    )):
        try:
            mount.run("mount", "--bind", "/run/test", "/run/test")
        except mount.MountError as exc:
            assert exc.stage == "bind"
            assert "TOP_SECRET" not in str(exc)
        else:
            raise AssertionError("Mount command failure was accepted")
    callback = (
        ROOT / "automation/ansible/callback_plugins/infra_nas_compact.py"
    ).read_text(encoding="utf-8")
    assert "NAS_URBACKUP_MOUNT_STAGE=" in callback
    assert "not-shared" in callback



def test_client_scoped_urbackup_archival_policy() -> None:
    """Never change unrelated clients or global server retention."""
    d = retention.desired_rules(7, 4)
    assert d["every_c0"] == "86400" and d["for_c0"] == str(7 * 86400)
    assert d["every_c1"] == "604800" and d["for_c1"] == str(4 * 604800)
    assert d["backup_type_c0"] == d["backup_type_c1"] == "file"
    refuses(lambda: retention.desired_rules(0, 4), retention.PolicyError)
    refuses(lambda: retention.desired_rules(7, 0), retention.PolicyError)
    refuses(lambda: retention.normalized_archive("archive=1&archive=2"), retention.PolicyError)

    config = {
        "client_name": "nas-local", "admin_user": "admin", "web_port": 55414,
        "keep_daily": 7, "keep_weekly": 4, "max_parallel_jobs": 1,
    }
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "retention.json"
        cfg.write_text(json.dumps(config), encoding="utf-8")
        assert retention.read_config(cfg) == config
        cfg.write_text(json.dumps({**config, "web_port": 55415}), encoding="utf-8")
        refuses(lambda: retention.read_config(cfg), retention.PolicyError)

    class Server:
        def __init__(self, initial="", use=1, *, registered=True):
            self.archive = initial
            self.use = use
            self.registered = registered
            self.calls = []

        def request(self, action, payload=None):
            self.calls.append((action, dict(payload or {})))
            if action == "status":
                return {"status": [
                    {"name": "other-machine", "id": 4},
                    *([{"name": "nas-local", "id": 12}] if self.registered else []),
                ]}
            if action == "settings" and payload["sa"] == "clientsettings":
                assert payload["t_clientid"] == "12"
                return {"settings": {"archive": {"value": self.archive, "use": self.use}}}
            if action == "settings" and payload["sa"] == "clientsettings_save":
                assert payload["t_clientid"] == "12"
                assert payload["archive.use"] == "2"
                assert set(payload) == {"sa", "t_clientid", "archive", "archive.use", "overwrite"}
                assert payload["overwrite"] == "true"
                self.archive, self.use = payload["archive"], 2
                return {"saved_ok": True}
            raise AssertionError("Unexpected or unsafe UrBackup API call")

    first = Server()
    refuses(lambda: retention.reconcile(first, config, apply=False), retention.PolicyError)
    assert all(call[1].get("sa") != "clientsettings_save" for call in first.calls)
    assert retention.reconcile(first, config, apply=True) is True
    assert retention.reconcile(first, config, apply=False) is False
    assert retention.reconcile(first, config, apply=True) is False
    assert retention.normalized_archive(first.archive) == d
    assert all(call[1].get("sa") not in ("general", "general_save")
               for call in first.calls)
    assert all(call[1].get("t_clientid", "12") == "12" for call in first.calls)
    conflict = Server(initial="every_c0=42", use=2)
    refuses(lambda: retention.reconcile(conflict, config, apply=True), retention.PolicyError)
    assert not any(call[1].get("sa") == "clientsettings_save" for call in conflict.calls)
    refuses(lambda: retention.reconcile(Server(registered=False), config, apply=True),
            retention.PolicyError)
    tasks = yaml.safe_load((ROOT / "automation/ansible/roles/nas/tasks/urbackup_client.yml").read_text())
    assert any(task.get("ansible.builtin.command", {}).get("argv") ==
               ["/usr/local/sbin/nas-urbackup-policy", "apply"]
               for task in tasks)
    assert any(task.get("ansible.builtin.command", {}).get("argv") ==
               ["/usr/local/sbin/nas-urbackup-policy", "verify"]
               for task in tasks)

def main() -> int:
    test_backup_policy()
    test_client_scoped_urbackup_archival_policy()
    test_volume_contract()
    test_live_guard_is_read_only()
    test_scripts_have_no_destructive_fallback()
    test_plain_state_archive_verification()
    test_docker_and_deploy_contract()
    test_permanent_docker_client_contract()
    test_mount_propagation_requires_verified_state()
    test_confirm_client_status_and_server_completed_pointer()
    test_bind_mount_detection_uses_findmnt_not_python_ismount()
    test_staging_mount_is_idempotent()
    test_mount_failure_output_has_no_arbitrary_stderr()
    print("[ОК] NAS UrBackup: политика, GUID, безопасность и новые задания")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
