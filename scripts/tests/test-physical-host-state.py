#!/usr/bin/env python3
"""Проверки сохранения доверия NAS при переносе из каталога блокировок."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_host_state import checked_record_path

SCRIPT = ROOT / "scripts/infra-manager/host/migrate-physical-host-state.py"
spec = importlib.util.spec_from_file_location("migrate_physical_state", SCRIPT)
assert spec and spec.loader
migrate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migrate
spec.loader.exec_module(migrate)


def refuses(action, exception=Exception):
    try:
        action()
    except exception:
        return
    raise AssertionError("Небезопасная операция была разрешена")


def fixture(root: Path):
    old = root / "locks/physical-hosts"
    new = root / "physical-hosts"
    old.mkdir(parents=True)
    new.mkdir()
    marker = {"name": "nas", "address": "192.0.2.22",
              "fingerprint": "SHA256:" + "B" * 43}
    storage = {"name": "nas", "address": "192.0.2.22",
               "fingerprint": "SHA256:" + "B" * 43,
               "devices": [{"role": "data", "serial": "HDD-A"}],
               "volumes": {"data": {"pool": "nas-data", "pool_guid": "123"}}}
    for name, content in (("nas.json", marker), ("nas-storage.json", storage)):
        (old / name).write_text(json.dumps(content), encoding="utf-8")
    return old, new


def test_migration_and_idempotency():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = fixture(root)
        # Прежние записи без переноса нельзя принимать за отсутствующие.
        refuses(lambda: checked_record_path("nas", data_root=root), InfraManagerError)
        refuses(lambda: checked_record_path("nas", storage=True, data_root=root),
                InfraManagerError)
        original = {entry.name: entry.read_bytes() for entry in old.iterdir()}
        assert migrate.migrate(old, new) == 2
        assert migrate.migrate(old, new) == 0
        for name, content in original.items():
            assert (old / name).read_bytes() == content
            assert (new / name).read_bytes() == content
            assert (new / name).stat().st_mode & 0o777 == 0o600
        assert checked_record_path("nas", data_root=root) == new / "nas.json"
        assert checked_record_path("nas", storage=True, data_root=root) == new / "nas-storage.json"
        # Конфликт не должен уничтожить ни одну из двух версий.
        (new / "nas.json").write_text('{"name":"nas","address":"other","fingerprint":"X"}')
        old_bytes = (old / "nas.json").read_bytes()
        new_bytes = (new / "nas.json").read_bytes()
        refuses(lambda: migrate.migrate(old, new), migrate.MigrationError)
        refuses(lambda: checked_record_path("nas", data_root=root), InfraManagerError)
        assert (old / "nas.json").read_bytes() == old_bytes
        assert (new / "nas.json").read_bytes() == new_bytes


def test_conflict_preflight_no_partial_copy():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = fixture(root)
        (new / "nas-storage.json").write_text(
            '{"name":"nas","devices":[],"volumes":{}}'
        )
        refuses(lambda: migrate.migrate(old, new), migrate.MigrationError)
        assert not (new / "nas.json").exists()
        assert (old / "nas.json").exists()


def test_bad_inputs():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = fixture(root)
        (old / "nas.json").write_text("{bad")
        refuses(lambda: migrate.migrate(old, new), migrate.MigrationError)
        assert not list(new.iterdir())
        (old / "nas.json").unlink()
        (old / "nas.json").symlink_to(old / "nas-storage.json")
        refuses(lambda: migrate.migrate(old, new), migrate.MigrationError)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = fixture(root)
        (new / "nas.json").symlink_to(old / "nas.json")
        refuses(lambda: migrate.migrate(old, new), migrate.MigrationError)
        refuses(lambda: checked_record_path("nas", data_root=root), InfraManagerError)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = fixture(root)
        (old / "unexpected.txt").write_text("keep")
        refuses(lambda: migrate.migrate(old, new), migrate.MigrationError)
        assert (old / "unexpected.txt").read_text() == "keep"
        assert not list(new.iterdir())

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old, new = fixture(root)
        for entry in old.iterdir():
            entry.unlink()
        old.rmdir()
        assert migrate.migrate(old, new) == 0
        assert checked_record_path("nas", data_root=root) == new / "nas.json"


def test_requires_real_directory():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        refuses(lambda: checked_record_path("nas", data_root=root), InfraManagerError)
        refuses(lambda: checked_record_path("../nas", data_root=root), InfraManagerError)
        (root / "physical-hosts").symlink_to(root)
        refuses(lambda: checked_record_path("nas", data_root=root), InfraManagerError)


def test_runtime_and_backup_contract():
    """Проверить одно и то же постоянное подключение у 910 и в Docker."""

    provision = yaml.safe_load(
        (ROOT / "infrastructure/guests/910-infra-manager/provision.yaml")
        .read_text(encoding="utf-8")
    )
    compose = yaml.safe_load(
        (ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/"
                "infra-manager/compose/docker-compose.yml").read_text(encoding="utf-8")
    )
    physical = "/mnt/persistent-state/physical-hosts"
    inside = "/var/lib/infra-manager/physical-hosts"
    binding = [
        item for item in provision["persistence"]["target_layout"]["state"]["bindings"]
        if item["source"] == physical
    ]
    assert len(binding) == 1
    assert binding[0]["target"] == inside
    assert binding[0]["mode"] == "0700"
    assert binding[0]["owner"] == "1001"
    volume = f"{physical}:{inside}"
    assert volume in provision["docker"]["services"]["runtime"]["mounts"]
    assert volume in compose["services"]["runtime"]["volumes"]
    assert inside in provision["persistence"]["backup_required"]

    tasks = (
        ROOT / "automation/ansible/roles/infra_manager/tasks/persistence.yml"
    ).read_text(encoding="utf-8")
    assert "scripts/infra-manager/host/migrate-physical-host-state.py" in tasks
    assert tasks.index("Создать каталоги постоянного состояния") < tasks.index(
        "Сохранить подтверждения физических машин"
    )


def main():
    test_migration_and_idempotency()
    test_conflict_preflight_no_partial_copy()
    test_bad_inputs()
    test_requires_real_directory()
    test_runtime_and_backup_contract()
    print("[ОК] Сохранение SSH-доверия и идентификаторов дисков NAS проверено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
