#!/usr/bin/env python3
"""Проверить безопасные границы резервирования, которое запускает 910."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import backup_controller
from infra_manager.common import InfraManagerError

exporter = ROOT / "scripts/infra-manager/host/backup-export.py"
spec = importlib.util.spec_from_file_location("infra_backup_exporter", exporter)
assert spec and spec.loader
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def fails(fn, name):
    try:
        fn()
    except (InfraManagerError, host.ExportError):
        return
    raise AssertionError(name)


def config(uuid="ABC-123", nas="//nas/share"):
    return {
        "schema_version": 1, "host": "pve", "job": "infra-core",
        "schedule": "03,09,15,21:30",
        "destinations": [
            {"id": "fast-ssd", "mountpoint": "/mnt/pve/backup-fast",
             "filesystem": "ext4", "repository": "infra-core", "uuid": uuid},
            {"id": "nas-backup", "mountpoint": "/mnt/pve/nas-backup",
             "filesystem": "nfs4", "repository": "pve-infra-core", "source": nas},
        ],
    }


def check_policy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "backup.yaml"
        with patch.object(backup_controller, "POLICY", path):
            path.write_text(yaml.safe_dump(config()), encoding="utf-8")
            raw, approved = backup_controller.policy()
            assert raw["host"] == "pve"
            assert approved == {"fast-ssd": "ABC-123", "nas-backup": "//nas/share"}
            # Отсутствие одного носителя допускается. Независимая вторая
            # копия должна отмечаться в статусе, а не выключать первую.
            path.write_text(yaml.safe_dump(config(uuid=None)), encoding="utf-8")
            with patch("infra_manager.pve_backup_ssd._read_state", return_value=None):
                _, single = backup_controller.policy()
            assert single == {"nas-backup": "//nas/share"}
            path.write_text(yaml.safe_dump(config(nas=None)), encoding="utf-8")
            _, single = backup_controller.policy()
            assert single == {"fast-ssd": "ABC-123"}
            path.write_text(yaml.safe_dump(config(uuid=None, nas=None)), encoding="utf-8")
            with patch("infra_manager.pve_backup_ssd._read_state", return_value=None):
                fails(backup_controller.policy, "Без обоих носителей запись запрещена")
            changed = config()
            changed["destinations"][0]["mountpoint"] = "/"
            path.write_text(yaml.safe_dump(changed), encoding="utf-8")
            fails(backup_controller.policy, "Нельзя перенаправлять restic в rootfs")
            changed = config()
            changed["destinations"][1]["repository"] = "../unsafe"
            path.write_text(yaml.safe_dump(changed), encoding="utf-8")
            fails(backup_controller.policy, "Нельзя подменять имя хранилища")


def check_host_denies_untrusted() -> None:
    fails(lambda: host.check_destinations({}), "Пустой список носителей запрещён")
    fails(lambda: host.check_destinations({"unknown": "bad"}), "Неизвестное назначение запрещено")
    with patch.object(host, "_mounted") as mounted:
        host.check_destinations({"fast-ssd": "disk-A"})
        assert mounted.call_count == 1
        host.check_destinations({"nas-backup": "nas-B"})
        assert mounted.call_count == 2
        host.check_destinations({"fast-ssd": "disk-A", "nas-backup": "nas-B"})
        assert mounted.call_count == 4
    fails(lambda: host.check_destinations(
        {"fast-ssd": "same", "nas-backup": "same"}),
        "SSD и NAS не должны совпадать")
    text = exporter.read_text(encoding="utf-8")
    assert "subprocess.run" in text and '"pct", "exec"' in text
    assert "semaphore-legacy.sqlite" not in text
    assert "_optional_sqlite" not in text
    assert 'TRANSITION_ACTIVATION = STATE / "locks/runtime-activation-pending"' in text
    assert 'ACTIVE_910_OPERATION = STATE / "operations/active/910.json"' in text
    assert '"/run"' in text
    for forbidden in ("apt-get", "mkfs.", "parted ", "systemctl enable",
                      '"restic", "backup"'):
        assert forbidden not in text, forbidden


def check_backup_blocks_runtime_activation() -> None:
    """Любой маркер и активный UUID должны блокировать снимок PVE."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        markers = (
            root / "operations/activation/runtime-activation-pending",
            root / "locks/runtime-activation-pending",
            root / "operations/active/910.json",
        )
        gate = root / "locks/backup-gate.lock"
        gate.parent.mkdir(parents=True)
        gate.write_text("", encoding="utf-8")
        for marker in markers:
            marker.parent.mkdir(parents=True, exist_ok=True)
        def try_snapshot() -> None:
            with host._consistent_window():
                pass

        with (
            patch.object(host, "INDEPENDENT_ACTIVATION", markers[0]),
            patch.object(host, "TRANSITION_ACTIVATION", markers[1]),
            patch.object(host, "ACTIVE_910_OPERATION", markers[2]),
            patch.object(host, "BACKUP_GATE", gate),
        ):
            # Снимок разрешён только при отсутствии всех трёх признаков.
            host._assert_no_runtime_activation()
            with host._consistent_window():
                pass
            for marker in markers:
                marker.write_text("test", encoding="utf-8")
                fails(host._assert_no_runtime_activation, "Нельзя копировать при активной операции")
                fails(try_snapshot, "Повторная проверка под backup-gate обязательна")
                marker.unlink()
                marker.symlink_to(gate)
                fails(host._assert_no_runtime_activation, "Нельзя обходить проверку символьной ссылкой")
                marker.unlink()


def check_run_does_not_claim_success() -> None:
    expected = config()
    with (
        patch.object(backup_controller, "policy", return_value=(expected, {"fast-ssd": "A", "nas-backup": "B"})),
        patch.object(backup_controller.shutil, "which", return_value="/usr/bin/restic"),
        patch.object(backup_controller, "_source_node", return_value="pve"),
        patch.object(backup_controller, "_remote", side_effect=InfraManagerError("не подтверждено")),
    ):
        fails(lambda: backup_controller.execute(run=True),
              "Провал preflight должен прекратить запись")
    text = backup_controller.__doc__
    assert "PVE" in text and "restic" in text


def check_one_ssd_target_runs_full_backup() -> None:
    """При отсутствии NAS получаем проверенный архив PVE и VM 100 только на SSD."""
    with tempfile.TemporaryDirectory() as root:
        work = Path(root) / "state"
        work.mkdir()
        original_tmp = tempfile.TemporaryDirectory
        calls = []
        def remote(_node, action, _payload=b""):
            calls.append(("remote", action))
            if action == "repositories":
                return SimpleNamespace(stdout=b'{"fast-ssd":"missing"}')
            if action == "password":
                return SimpleNamespace(stdout=b"K" * 48 + b"\n")
            if action == "check":
                return SimpleNamespace(stdout=b"[OK]")
            raise AssertionError("Unexpected remote action")
        with (
            patch.object(backup_controller, "policy",
                         return_value=(config(nas=None), {"fast-ssd": "A-UUID"})),
            patch.object(backup_controller, "_source_node", return_value="pve"),
            patch.object(backup_controller.shutil, "which", return_value="/usr/bin/restic"),
            patch.object(backup_controller, "_remote", side_effect=remote),
            patch.object(backup_controller, "OperationStore",
                         return_value=SimpleNamespace(settings=work)),
            patch.object(backup_controller, "_restic",
                         side_effect=lambda _, dst, __, args, **kw: calls.append((dst, args[0]))),
            patch.object(backup_controller, "_stream_snapshot",
                         side_effect=lambda _, __, ___, dst, **kw:
                         calls.append((dst, "vm100" if kw.get("vm100") else "core"))),
            patch.object(backup_controller, "_vm100_due", return_value=True),
            patch.object(backup_controller.tempfile, "TemporaryDirectory",
                         side_effect=lambda *a, **kw: original_tmp()),
            patch("infra_manager.pve.PveClient"),
            patch("infra_manager.pve_backup_ssd.reconcile_backup_ssd",
                  return_value="A-UUID"),
        ):
            backup_controller.execute(run=True)
        assert not any(item[0] == "nas-backup" for item in calls)
        assert ("fast-ssd", "core") in calls
        assert ("fast-ssd", "vm100") in calls
        record = json.loads((work / "backup-last-full.json").read_text())
        assert record["destinations"] == ["fast-ssd"]
        assert record["includes_vm100"] is True



def check_pve_safe_failure_messages() -> None:
    """Каждый отсутствующий PVE-only компонент называется без секретных данных."""
    required = (
        "unseal OpenBao", "аварийный GitHub-ключ",
        "AppRole снимка OpenBao", "помощник Raft-снимка",
        "идентичность PVE-гостя 910",
    )
    for label in required:
        secret = ("[ОШИБКА] Архив не выдан: Не подготовлен PVE-only компонент: "
                  + label + "\n").encode()
        assert backup_controller._pve_check_error(secret) == (
            backup_controller.PVE_CHECK_ERRORS[label]
        )
    assert "secret-password" not in backup_controller._pve_check_error(
        b"[ERROR] secret-password"
    )
    assert "secret-password" not in backup_controller._pve_check_error(
        b"[ERROR] secret-password" * 1000
    )
    assert "не хватает обязательного" in backup_controller._pve_check_error(
        "[ОШИБКА] Архив не выдан: Неполное PVE-only состояние для резервирования".encode()
    )

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        state = root / "state"
        state.mkdir()
        keys = root / "pve-only"
        files = (
            ("unseal OpenBao", keys / "openbao/unseal.key"),
            ("аварийный GitHub-ключ", keys / "recovery/github_proxmox_repo_ed25519"),
            ("AppRole снимка OpenBao", keys / "openbao/backup-access.json"),
            ("помощник Raft-снимка", root / "helper.py"),
            ("идентичность PVE-гостя 910", root / "identity.json"),
        )
        for _, path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("safe-test", encoding="utf-8")
        with (
            patch.object(host, "ROOT", root),
            patch.object(host, "STATE", state),
            patch.object(host, "ROLE", files[2][1]),
            patch.object(host, "RAFT_HELPER", files[3][1]),
            patch.object(host, "PVE_IDENTITY", files[4][1]),
            patch.object(host, "SOURCES", ()),
            patch.object(Path, "is_mount", return_value=True),
        ):
            for label, path in files:
                path.unlink()
                try:
                    host.check_sources()
                except host.ExportError as exc:
                    assert str(exc) == "Не подготовлен PVE-only компонент: " + label
                    assert str(path) not in str(exc)
                else:
                    raise AssertionError(f"Missing {label} was accepted")
                path.write_text("safe-test", encoding="utf-8")



def check_restic_and_export_failures() -> None:
    """Различать ошибки источника и хранилища без публикации секретов."""
    import io

    class ResticFailure:
        def __init__(self, diagnostics):
            diagnostics.write(b"no space left on device; TOKEN=private-secret")

        def wait(self, timeout=None):
            return 1

    with (
        patch.object(backup_controller.subprocess, "Popen",
                     side_effect=lambda _cmd, **kw: ResticFailure(kw["stderr"])),
        patch.object(backup_controller, "PATHS",
                     SimpleNamespace(pve_host_private_key=Path("/key"),
                                     pve_host_known_hosts=Path("/known"))),
    ):
        try:
            backup_controller._restic("pve", "fast-ssd", Path("/password"), ["backup"])
        except InfraManagerError as exc:
            message = str(exc)
            assert "код 1" in message and "закончилось место" in message
            assert "private-secret" not in message
        else:
            raise AssertionError("Restic failed without an exception")

    class Export:
        def __init__(self, code, diagnostics, error):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"archive")
            diagnostics.write(error)
            self.returncode = None
            self.code = code

        def wait(self, timeout=None):
            self.returncode = self.code
            return self.code

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def scenario(code, stderr, restic_error=None):
        def popen(_cmd, **kwargs):
            return Export(code, kwargs["stderr"], stderr)
        with (
            patch.object(backup_controller.subprocess, "Popen", side_effect=popen),
            patch.object(backup_controller, "_ssh_command", return_value=["ssh"]),
            patch.object(backup_controller, "_restic", side_effect=restic_error),
        ):
            try:
                backup_controller._stream_snapshot(
                    "pve", {"fast-ssd": "UUID"}, Path("/key"), "fast-ssd")
            except InfraManagerError as exc:
                return str(exc)
            return None

    marker = "[ОШИБКА] Архив не выдан: Не удалось собрать согласованный архив PVE\n".encode()
    msg = scenario(1, marker + b"secret=private", InfraManagerError("restic failed"))
    assert "TAR-архив" in msg and "private" not in msg
    msg = scenario(255, b"bad transport private-secret")
    assert "SSH-соединение" in msg and "private" not in msg
    msg = scenario(-13, b"broken pipe private-secret", InfraManagerError("restic failed"))
    assert msg == "restic failed"
    msg = scenario(0, b"", InfraManagerError("restic failed"))
    assert msg == "restic failed"
    assert scenario(0, b"") is None
    assert "unknown-password" not in backup_controller._restic_cause(
        b"unknown-password from stderr")
    service = (ROOT / "infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/"
               "infra-manager-backup.service").read_text()
    assert "CacheDirectory=infra-manager-restic" in service
    assert "RESTIC_CACHE_DIR=/var/cache/infra-manager-restic" in service
    assert "CacheDirectoryMode=0700" in service


def main():
    check_policy()
    check_restic_and_export_failures()
    check_host_denies_untrusted()
    check_pve_safe_failure_messages()
    check_backup_blocks_runtime_activation()
    check_run_does_not_claim_success()
    check_one_ssd_target_runs_full_backup()
    print("[ОК] Резервированием управляет 910; неподтверждённые SSD/NAS не изменяются")


if __name__ == "__main__":
    main()
