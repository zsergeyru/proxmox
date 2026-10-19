#!/usr/bin/env python3
"""Автономные испытания общего формата дисков PVE и NAS без доступа к носителям."""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.storage_catalog import (
    load_physical_storage, require_declared_identifiers, storage_path,
    validate_storage,
)


def device(name: str, role: str, serial: str | None = None) -> dict:
    return {
        "id": name, "role": role, "model": "Test model", "capacity_gb": 1000,
        "serial": serial, "by_id": f"/dev/disk/by-id/test-{name}" if serial else None,
    }


def pve_example() -> dict:
    """Два независимых логических тома могут делить один NVMe."""

    return {
        "schema_version": 1,
        "host": "pve",
        "devices": [device("nvme", "system", "NVME1")],
        "volumes": [
            {
                "id": name, "kind": "lvm_thin", "device": "nvme",
                "volume_group": "pve", "thin_pool": "data", "size_gb": size,
                "filesystem": "ext4",
                "filesystem_uuid": f"00000000-0000-0000-0000-{size:012d}",
            }
            for name, size in (("infra_state", 40), ("gogs_data", 30))
        ],
        "mounts": [
            {"volume": "infra_state", "path": "/mnt/bindmounts/infra-state"},
            {"volume": "gogs_data", "path": "/mnt/bindmounts/gogs-data"},
        ],
        "policies": {
            "existing_data": "preserve",
            "unverified_devices": "reject_writes",
            "missing_mount": "reject_writes",
        },
    }


def nas_example() -> dict:
    """Два независимых однодисковых ZFS-пула и отдельный ext4."""

    data = pve_example()
    data.update({
        "host": "nas",
        "devices": [
            device("system", "system", "SSD1"),
            device("shared", "shared", "HDD1"),
            device("data", "data", "HDD2"),
            device("backup", "backup", "HDD3"),
        ],
        "volumes": [
            {
                "id": "shared", "kind": "ext4", "device": "shared",
                "filesystem_uuid": "10000000-0000-0000-0000-000000000001",
                "omv_reference": "20000000-0000-0000-0000-000000000001",
            },
            *[{
                "id": role, "kind": "zfs_pool", "device": role,
                "pool": "nas-" + role, "pool_guid": guid,
                "omv_reference": f"20000000-0000-0000-0000-{guid:0>12}",
            }
            for role, guid in (("data", "1111111"), ("backup", "2222222"))],
        ],
        "mounts": [
            {"volume": "shared", "path": "/srv/dev-disk-by-uuid-aabbcc"},
            {"volume": "data", "path": "/nas-data"},
            {"volume": "backup", "path": "/nas-backup"},
        ],
    })
    return data


def refuses(operation) -> None:
    """Опасная настройка должна быть отвергнута без исполнения команд."""

    try:
        operation()
    except InfraManagerError:
        return
    raise AssertionError("Ошибка машинного описания не обнаружена")


def test_schema_and_loading(repo: Path) -> None:
    """Проверить один загрузчик для разных каталогов PVE и NAS."""

    for name, sample in (("pve", pve_example()), ("nas", nas_example())):
        path = storage_path(repo, name)
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(sample, allow_unicode=True), encoding="utf-8")
        parsed = load_physical_storage(repo, name)
        assert parsed.host == name
        assert parsed.manifest == path
        assert parsed.volume(sample["volumes"][0]["id"]) == sample["volumes"][0]
        require_declared_identifiers(parsed, omv=name == "nas")
        refuses(lambda: parsed.volume("missing"))
    for invalid in ("../nas", "NAS", "/etc/passwd", "nas/subdir", "nas;id"):
        refuses(lambda invalid=invalid: storage_path(repo, invalid))
    refuses(lambda: load_physical_storage(repo, "missing"))

    # Ссылки вместо файла описания никогда не раскрываются загрузчиком.
    nas = storage_path(repo, "nas")
    nas.unlink()
    nas.symlink_to(storage_path(repo, "pve"))
    refuses(lambda: load_physical_storage(repo, "nas"))


def test_invalid_definitions(repo: Path) -> None:
    """Схема и перекрёстные проверки должны отклонять опасные значения."""

    original = nas_example()

    def invalid(change) -> None:
        document = copy.deepcopy(original)
        change(document)
        refuses(lambda: validate_storage(repo, "nas", document))

    invalid(lambda d: d.update({"host": "pve"}))
    invalid(lambda d: d.update({"password": "secret"}))
    invalid(lambda d: d["policies"].update({"existing_data": "erase"}))
    invalid(lambda d: d["devices"][1].update({"by_id": "/dev/sda"}))
    invalid(lambda d: d["devices"][1].update({"serial": "SSD1"}))
    invalid(lambda d: d["devices"][1].update({"by_id": d["devices"][2]["by_id"]}))
    invalid(lambda d: d["devices"][1].update({"id": "data"}))
    invalid(lambda d: d["devices"][1].update({"role": "system"}))
    invalid(lambda d: d["volumes"][0].update({"device": "absent"}))
    invalid(lambda d: d["volumes"][0].update({"device": "system"}))
    invalid(lambda d: d["volumes"][0].update({"filesystem_uuid": "not-uuid"}))
    invalid(lambda d: d["volumes"][1].update({"pool": "nas-backup"}))
    invalid(lambda d: d["volumes"][2].update({"device": "data"}))
    invalid(lambda d: d["mounts"][0].update({"volume": "missing"}))
    invalid(lambda d: d["mounts"][0].update({"path": "/"}))
    invalid(lambda d: d["mounts"][0].update({"path": "/mnt/../bad"}))
    invalid(lambda d: d["mounts"][0].update({"path": "/nas-data/subdir"}))
    invalid(lambda d: d["mounts"][0].update({"path": "/nas-data"}))
    invalid(lambda d: d["mounts"].pop())

    # Неизвестный серийный номер допустим при планировании, но не для записи.
    unconfirmed = copy.deepcopy(original)
    unconfirmed["devices"][1]["serial"] = None
    unconfirmed["devices"][1]["by_id"] = None
    parsed = validate_storage(repo, "nas", unconfirmed)
    refuses(lambda: require_declared_identifiers(parsed, omv=True))
    unconfirmed = copy.deepcopy(original)
    unconfirmed["volumes"][1]["pool_guid"] = None
    parsed = validate_storage(repo, "nas", unconfirmed)
    refuses(lambda: require_declared_identifiers(parsed, omv=True))
    unconfirmed = copy.deepcopy(original)
    unconfirmed["mounts"][0]["path"] = None
    parsed = validate_storage(repo, "nas", unconfirmed)
    refuses(lambda: require_declared_identifiers(parsed, omv=True))
    unconfirmed = copy.deepcopy(original)
    unconfirmed["volumes"][0]["omv_reference"] = None
    parsed = validate_storage(repo, "nas", unconfirmed)
    require_declared_identifiers(parsed)
    refuses(lambda: require_declared_identifiers(parsed, omv=True))


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        schema = ROOT / "infrastructure/schemas/physical-storage.schema.yaml"
        target = root / "infrastructure/schemas/physical-storage.schema.yaml"
        target.parent.mkdir(parents=True)
        target.write_bytes(schema.read_bytes())
        test_schema_and_loading(root)
        test_invalid_definitions(root)
    print("[ОК] Общие описания PVE/NAS проверены без обращения к дискам")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
