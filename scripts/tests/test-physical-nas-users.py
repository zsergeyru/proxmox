#!/usr/bin/env python3
"""Проверки общего каталога и локальных правил SMB, без реальных секретов."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.user_catalog import load_user_catalog, validate_user_catalog
from infra_manager.physical_nas_users import (
    load_nas_users, require_nas_users_configured, validate_nas_access,
)


def refuses(callback) -> None:
    """Ожидать отказ от некорректных имён и прав до любого изменения NAS."""

    try:
        callback()
    except InfraManagerError:
        return
    raise AssertionError("Ошибочная конфигурация пользователей/прав принята")


def main() -> int:
    """Убедиться, что каталог общий и локальная политика не дублирует людей."""

    catalog = load_user_catalog(ROOT)
    assert catalog["schema_version"] == 1
    assert catalog["groups"] == ["admins", "family", "guests"]
    assert catalog["users"] == []
    merged = load_nas_users(ROOT)
    assert merged["groups"] == catalog["groups"]
    assert merged["users"] == catalog["users"]
    assert set(merged["shares"]) == {"shared", "movies", "music", "data"}
    assert all(
        not merged["shares"][share]["read_groups"]
        and not merged["shares"][share]["write_groups"]
        for share in merged["shares"]
    )
    refuses(lambda: require_nas_users_configured(ROOT))

    access_path = ROOT / "infrastructure/hosts/nas/access.yaml"
    rights = yaml.safe_load(access_path.read_text(encoding="utf-8"))
    assert not (set(rights) & {"users", "groups"})

    ready_catalog = copy.deepcopy(catalog)
    ready_catalog["users"] = [
        {"name": "sampleuser", "groups": ["family"]},
        {"name": "samplemanager", "groups": ["admins", "family"]},
    ]
    ready_rights = copy.deepcopy(rights)
    ready_rights["shares"]["shared"]["write_groups"] = ["family"]
    ready_rights["shares"]["data"]["write_groups"] = ["admins"]
    assert validate_user_catalog(ROOT, ready_catalog) == ready_catalog
    assert validate_nas_access(ROOT, ready_rights, ready_catalog) == ready_rights

    invalid = copy.deepcopy(ready_catalog)
    invalid["users"][0]["password"] = "not-allowed-in-public-git"
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["users"][0]["password_hash"] = "secret-hash"
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["users"][0]["groups"] = ["not-existing"]
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["users"][1]["name"] = "sampleuser"
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["users"][0]["name"] = "root"
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["users"][0]["name"] = "admin"
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["groups"] = ["admins", "family", "family"]
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_catalog)
    invalid["groups"] = ["family", "guests"]
    refuses(lambda: validate_user_catalog(ROOT, invalid))

    invalid = copy.deepcopy(ready_rights)
    invalid["shares"]["music"]["read_groups"] = ["not-existing"]
    refuses(lambda: validate_nas_access(ROOT, invalid, ready_catalog))

    invalid = copy.deepcopy(ready_rights)
    invalid["users"] = ready_catalog["users"]
    refuses(lambda: validate_nas_access(ROOT, invalid, ready_catalog))

    invalid = copy.deepcopy(ready_rights)
    invalid["shares"]["data"]["password"] = "not-in-git"
    refuses(lambda: validate_nas_access(ROOT, invalid, ready_catalog))

    provision = yaml.safe_load(
        (ROOT / "infrastructure/guests/140-web-gateway/provision.yaml")
        .read_text(encoding="utf-8")
    )
    allowed = provision["web_gateway"]["authelia"]["authentication"]["groups"]
    assert set(allowed) == set(catalog["groups"])

    print("[ОК] Пользователи общие, права локальны, Authelia согласована, секретов нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
