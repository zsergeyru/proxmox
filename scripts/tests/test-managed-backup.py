#!/usr/bin/env python3
"""Проверить безопасные границы резервирования, которое запускает 910."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
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
            path.write_text(yaml.safe_dump(config(uuid=None)), encoding="utf-8")
            fails(backup_controller.policy, "Без SSD запуск должен блокироваться")
            path.write_text(yaml.safe_dump(config(nas=None)), encoding="utf-8")
            fails(backup_controller.policy, "Без NAS запуск должен блокироваться")
            changed = config()
            changed["destinations"][0]["mountpoint"] = "/"
            path.write_text(yaml.safe_dump(changed), encoding="utf-8")
            fails(backup_controller.policy, "Нельзя перенаправлять restic в rootfs")
            changed = config()
            changed["destinations"][1]["repository"] = "../unsafe"
            path.write_text(yaml.safe_dump(changed), encoding="utf-8")
            fails(backup_controller.policy, "Нельзя подменять имя хранилища")


def check_host_denies_untrusted() -> None:
    fails(lambda: host.check_destinations({"fast-ssd": "fake"}),
          "Необходимо подтверждение обоих устройств")
    with patch.object(host, "_mounted") as mounted:
        host.check_destinations({"fast-ssd": "disk-A", "nas-backup": "nas-B"})
        assert mounted.call_count == 2
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


def main():
    check_policy()
    check_host_denies_untrusted()
    check_backup_blocks_runtime_activation()
    check_run_does_not_claim_success()
    print("[ОК] Резервированием управляет 910; неподтверждённые SSD/NAS не изменяются")


if __name__ == "__main__":
    main()
