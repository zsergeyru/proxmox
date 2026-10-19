#!/usr/bin/env python3
"""Обязательный контракт: один пользовательский интерфейс и безопасное резервирование."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/backup/backup-reconcile.py"
spec = importlib.util.spec_from_file_location("test_integrated_backup_reconcile", SOURCE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_project_rules() -> None:
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for term in ("infra-manager", "storage.yaml", "backup.yaml", "deploy 910", "status 910", "Запрещено", "сохран"):
        assert term in rules, term
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[AGENTS.md](AGENTS.md)" in readme
    assert not (ROOT / "scripts/backup/infra-manager-backup.py").exists()
    assert not (ROOT / "scripts/infra-manager/host/infra_manager_backup.py").exists()
    manager = (ROOT / "scripts/infra-manager/host/manager.py").read_text(encoding="utf-8")
    assert 'arguments[0] == "backup"' not in manager
    operator = (ROOT / "scripts/infra-manager/infra_manager/operator.py").read_text(encoding="utf-8")
    assert 'read_backup_host_status(_pve_node())' in operator
    assert 'subparsers.add_parser("backup")' not in operator
    host = (ROOT / "scripts/infra-manager/infra_manager/pve_host.py").read_text(encoding="utf-8")
    assert 'OPERATOR_HOST_COMMAND.with_name("infra-manager-backup")' not in host
    assert "infra-manager-backup-reconcile.timer" in host
    guest = (ROOT / "scripts/infra-manager/infra_manager/guest_deploy.py").read_text(encoding="utf-8")
    assert "install_backup_host_support(context.node, repo_root)" in guest


def test_unverified_disk_is_never_modified() -> None:
    with (
        patch.object(module, "readiness_lock", return_value=lambda: None),
        patch.object(module, "reconcile_config", return_value=(
            {"destinations": [], "host": "pve"}, {"devices": [
                {"id": "backup_ssd", "role": "backup", "serial": None, "by_id": None},
            ]},
        )),
        patch.object(module, "save_report") as report,
        patch.object(module, "prepare_nas") as nas,
        patch.object(module, "ready") as write,
    ):
        module.reconcile()
    nas.assert_not_called()
    write.assert_not_called()
    assert report.call_args.args[0] == "pending"
    assert "Нет подтверждения SSD" in report.call_args.args[1]


def test_config_is_idempotent_and_keeps_observed_uuid() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        desired = root / "desired.yaml"
        current = root / "active.yaml"
        devices = root / "storage.yaml"
        policy = {
            "schema_version": 1, "host": "pve", "destinations": [
                {"id": "fast-ssd", "uuid": None, "source": None},
                {"id": "nas-backup", "uuid": None, "source": "nas:/dataset"},
            ],
        }
        old = {
            "schema_version": 1, "host": "pve", "destinations": [
                {"id": "fast-ssd", "uuid": "observed-uuid", "source": None},
                {"id": "nas-backup", "uuid": None, "source": "nas:/dataset"},
            ],
        }
        desired.write_text(yaml.safe_dump(policy), encoding="utf-8")
        current.write_text(yaml.safe_dump(old), encoding="utf-8")
        devices.write_text(yaml.safe_dump({"schema_version": 1, "host": "pve"}), encoding="utf-8")
        with (
            patch.object(module, "DESIRED", desired),
            patch.object(module, "DEVICES", devices),
            patch.object(module, "CURRENT", current),
        ):
            merged, _ = module.reconcile_config()
            assert merged["destinations"][0]["uuid"] == "observed-uuid"
            same, _ = module.reconcile_config()
            assert same == merged
            policy["destinations"][0]["uuid"] = "other-device"
            desired.write_text(yaml.safe_dump(policy), encoding="utf-8")
            try:
                module.reconcile_config()
            except RuntimeError as exc:
                assert "подтверждение нового носителя" in str(exc)
            else:
                raise AssertionError("Изменённый UUID нельзя подменить молча")


def test_safe_after_missing_nas() -> None:
    with (
        patch.object(module, "readiness_lock", return_value=lambda: None),
        patch.object(module, "reconcile_config", return_value=({}, {})),
        patch.object(module, "prepare_ssd"),
        patch.object(module, "prepare_nas", side_effect=RuntimeError("Нет подключения NAS")),
        patch.object(module, "ready") as write,
        patch.object(module, "save_report") as report,
    ):
        module.reconcile()
    write.assert_not_called()
    assert report.call_args.args[0] == "pending"


def test_no_raw_key_exposure() -> None:
    config = yaml.safe_load(
        (ROOT / "infrastructure/hosts/pve/backup.yaml").read_text(encoding="utf-8")
    )
    assert config["initialize_blank_ssd_serial"] is None
    assert config["recovery_key_destination"] is None
    units = (ROOT / "scripts/backup/infra-manager-backup-reconcile.timer").read_text()
    assert "OnUnitInactiveSec=1h" in units
    assert "infra-manager-backup-reconcile.service" in units


if __name__ == "__main__":
    test_project_rules()
    test_unverified_disk_is_never_modified()
    test_config_is_idempotent_and_keeps_observed_uuid()
    test_safe_after_missing_nas()
    test_no_raw_key_exposure()
    print("[ОК] Единый интерфейс и безопасное согласование резервирования подтверждены")
