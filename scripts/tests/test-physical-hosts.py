#!/usr/bin/env python3
"""Проверить каталог физических машин и остановку на опасных настройках."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_hosts import list_physical_hosts, load_physical_host
from infra_manager.physical_host_operations import require_host_access


def check_contract() -> None:
    """Проверить машинное описание NAS в репозитории."""

    hosts = list_physical_hosts(ROOT)
    assert "nas" in {host.name for host in hosts}
    nas = load_physical_host(ROOT, "nas")
    assert nas.address == "192.168.1.12"
    assert nas.driver == "omv8"
    assert nas.ssh_host_fingerprint is None
    assert [item["role"] for item in nas.devices] == [
        "system", "shared", "data", "backup"
    ]


def check_invalid_names() -> None:
    """Не позволять выбирать файлы вне каталога физических машин."""

    for name in ("../nas", "/tmp", "nas/../../", "NAS", "nas;rm"):
        try:
            load_physical_host(ROOT, name)
        except InfraManagerError:
            continue
        raise AssertionError(f"Опасное имя прошло проверку: {name}")


def check_disk_identity() -> None:
    """Отказать при дубликатах и порче отдельно вынесенного storage.yaml."""

    host_path = ROOT / "infrastructure/hosts/nas/host.yaml"
    storage_path = ROOT / "infrastructure/hosts/nas/storage.yaml"
    host_data = yaml.safe_load(host_path.read_text(encoding="utf-8"))
    storage_data = yaml.safe_load(storage_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target_dir = root / "infrastructure/hosts/nas"
        schema_dir = root / "infrastructure/schemas"
        target_dir.mkdir(parents=True)
        schema_dir.mkdir(parents=True)
        for name in ("physical-host", "physical-storage"):
            filename = f"{name}.schema.yaml"
            (schema_dir / filename).write_bytes(
                (ROOT / "infrastructure/schemas" / filename).read_bytes()
            )
        host_target = target_dir / "host.yaml"
        storage_target = target_dir / "storage.yaml"

        def write(host, storage):
            host_target.write_text(
                yaml.safe_dump(host, allow_unicode=True), encoding="utf-8"
            )
            storage_target.write_text(
                yaml.safe_dump(storage, allow_unicode=True), encoding="utf-8"
            )

        write(host_data, storage_data)
        assert len(load_physical_host(root, "nas").devices) == 4

        # The one-time reinitialization policy MUST NOT be able to select
        # the installed OS disk or any unknown physical disk.
        for forbidden in ("system", "unknown", "unused"):
            policy = yaml.safe_load(storage_path.read_text(encoding="utf-8"))
            policy["initialization"]["recreate_once"] = ["data", forbidden]
            write(host_data, policy)
            try:
                load_physical_host(root, "nas")
            except InfraManagerError:
                pass
            else:
                raise AssertionError(
                    "Unsafe one-time recreation target accepted: " + forbidden
                )
        write(host_data, storage_data)
        duplicate = yaml.safe_load(storage_target.read_text(encoding="utf-8"))
        duplicate["devices"][1]["serial"] = "DUPLICATE"
        duplicate["devices"][2]["serial"] = "DUPLICATE"
        write(host_data, duplicate)
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Повторяющийся серийный номер принят")

        pinned = yaml.safe_load(storage_path.read_text(encoding="utf-8"))
        pinned["devices"][1]["by_id"] = "/dev/disk/by-id/ata-unchecked"
        write(host_data, pinned)
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Непроверенный by-id прошёл к исполнителю NAS")

        invalid = yaml.safe_load(storage_path.read_text(encoding="utf-8"))
        invalid["host"] = "pve"
        write(host_data, invalid)
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Подмена имени хранилища принята")

        invalid = yaml.safe_load(storage_path.read_text(encoding="utf-8"))
        invalid["volumes"][0]["device"] = "system"
        write(host_data, invalid)
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Подмена системного диска принята")

        wrong_host = dict(host_data, name="other")
        write(wrong_host, storage_data)
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Подмена имени физического узла принята")

        wrong_host = dict(host_data, storage={})
        write(wrong_host, storage_data)
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Повторная секция storage в host.yaml принята")

        write(host_data, storage_data)
        storage_target.unlink()
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Отсутствующий storage.yaml был принят")


def main() -> int:
    """Выполнить проверки без доступа к физическому NAS."""

    check_contract()
    check_invalid_names()
    check_disk_identity()
    require_host_access(ROOT, "nas")
    print("[ОК] Машинные описания физических узлов проверены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
