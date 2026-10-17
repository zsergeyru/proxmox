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
    assert nas.address is None
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
    """Запретить повторяющиеся серийные номера и подмену имени."""

    manifest = ROOT / "infrastructure" / "hosts" / "nas" / "host.yaml"
    schema = ROOT / "infrastructure" / "schemas" / "physical-host.schema.yaml"
    base = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "infrastructure" / "hosts" / "nas").mkdir(parents=True)
        (root / "infrastructure" / "schemas").mkdir(parents=True)
        (root / "infrastructure" / "schemas" / schema.name).write_bytes(
            schema.read_bytes()
        )
        target = root / "infrastructure" / "hosts" / "nas" / "host.yaml"
        base["storage"]["devices"][1]["serial"] = "DUPLICATE"
        base["storage"]["devices"][2]["serial"] = "DUPLICATE"
        target.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Повторяющийся серийный номер принят")
        base["storage"]["devices"][2]["serial"] = "DIFFERENT"
        base["name"] = "other"
        target.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
        try:
            load_physical_host(root, "nas")
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Подмена имени физического узла принята")


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
