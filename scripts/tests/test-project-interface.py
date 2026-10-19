#!/usr/bin/env python3
"""Контракт: Semaphore выводится, PVE минимален, копии не выдают за проверенные."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/infra-manager/host/backup-status.py"
spec = importlib.util.spec_from_file_location("infra_backup_host_status", SOURCE)
assert spec is not None and spec.loader is not None
status_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status_module)


def test_project_contract() -> None:
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for term in ("infra-manager", "infra-web", "без", "Semaphore", "storage.yaml",
                 "backup.yaml", "не обязателен", "PVE", "разметк"):
        assert term in rules, term
    assert "задания Semaphore в 910" not in rules
    host = (ROOT / "scripts/infra-manager/infra_manager/pve_host.py").read_text(
        encoding="utf-8"
    )
    section = host.split("def install_backup_host_support(", 1)[1].split(
        "def install_operator_host_support(", 1
    )[0]
    assert "backup-status.py" in section
    for forbidden in ("apt-get", "systemctl", "backup-reconcile", "backup-device.py",
                      "backup-mount.py", "backup-setup.py", "backup-pve.py"):
        assert forbidden not in section, forbidden

    assert not (ROOT / "scripts/backup/backup-reconcile.py").exists()
    assert not (ROOT / "scripts/backup/infra-manager-backup-reconcile.timer").exists()
    assert not (ROOT / "scripts/backup/infra-manager-backup-reconcile.service").exists()
    assert not (ROOT / "scripts/backup/infra-manager-backup.py").exists()
    assert not (ROOT / "scripts/infra-manager/host/infra_manager_backup.py").exists()
    shell = (ROOT / "scripts/infra-manager/host/manager.py").read_text(
        encoding="utf-8"
    )
    assert 'arguments[0] == "backup"' not in shell
    guest = (ROOT / "scripts/infra-manager/infra_manager/guest_deploy.py").read_text(
        encoding="utf-8"
    )
    assert "install_backup_host_support(context.node, repo_root)" in guest

    source = (ROOT / "scripts/backup/backup-setup.py").read_text(encoding="utf-8")
    enable = source.split("def enable() -> None:", 1)[1].split(
        "def main() -> int:", 1
    )[0]
    assert "not CONFIRMATION.is_file()" not in enable
    config = (ROOT / "infrastructure/hosts/pve/backup.yaml").read_text(
        encoding="utf-8"
    )
    assert "initialize_blank_ssd_serial:" not in config
    assert "recovery_key_destination:" not in config


def test_status_is_not_unsupported_success() -> None:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        fast = root / "fast"
        nas = root / "nas"
        (fast / "infra-core").mkdir(parents=True)
        (nas / "pve-infra-core").mkdir(parents=True)
        last = root / "last-success.json"
        vm100 = root / "last-vm100.json"
        key = root / "not-exported-key"
        approval = root / "not-restored"
        with (
            patch.object(status_module, "LAST_SUCCESS", last),
            patch.object(status_module, "LAST_VM100", vm100),
            patch.object(status_module, "FAST_MOUNT", fast),
            patch.object(status_module, "NAS_MOUNT", nas),
            patch.object(status_module, "KEY_EXPORTED", key),
            patch.object(status_module, "RESTORE_APPROVED", approval),
            patch.object(Path, "is_mount", return_value=True),
        ):
            initial = status_module.report(now)
            assert "Нет подтверждённой полной копии" in initial
            assert "не блокирует резервирование" in initial

            last.write_text(json.dumps({
                "created_utc": now.isoformat(),
                "destinations": [str(fast / "infra-core"), str(nas / "pve-infra-core")],
            }), encoding="utf-8")
            vm100.write_text(json.dumps({
                "date": now.date().isoformat(),
            }), encoding="utf-8")
            fresh = status_module.report(now)
            assert "Последняя полная копия" in fresh
            assert "целостность не перепроверена" in fresh
            assert "Восстановление из архива не испытано" in fresh
            assert "не блокирует резервирование" in fresh

            last.write_text(json.dumps({
                "created_utc": (now - timedelta(hours=11)).isoformat(),
                "destinations": [str(fast / "infra-core"), str(nas / "pve-infra-core")],
            }), encoding="utf-8")
            stale = status_module.report(now)
            assert "устарела" in stale
            last.write_text("not-json", encoding="utf-8")
            assert "повреждена" in status_module.report(now)


if __name__ == "__main__":
    test_project_contract()
    test_status_is_not_unsupported_success()
    print("[ОК] PVE без новых пакетов и таймеров; статус и ключ не дают ложной готовности")
