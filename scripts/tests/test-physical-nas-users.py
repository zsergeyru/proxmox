#!/usr/bin/env python3
"""Проверки общего каталога и локальных правил SMB, без реальных секретов."""

from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.user_catalog import load_user_catalog, validate_user_catalog
from infra_manager.physical_nas_users import (
    load_nas_users, require_nas_users_configured, require_nas_smb_secret,
    store_nas_smb_secret, validate_nas_access,
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
    assert catalog["users"] == [
        {"name": "sergey", "groups": ["admins"]},
        {"name": "sveta", "groups": ["family"]},
        {"name": "danya", "groups": ["family"]},
    ]
    merged = load_nas_users(ROOT)
    assert merged["groups"] == catalog["groups"]
    assert merged["users"] == catalog["users"]
    assert set(merged["shares"]) == {"shared", "movies", "music", "data"}
    assert merged["schema_version"] == 3
    assert merged["mode"] == "named_users"
    assert merged["credential"]["source"] == "controller_file"
    assert all(rights == {"access": "read_write"} for rights in merged["shares"].values())
    assert require_nas_users_configured(ROOT) == merged

    access_path = ROOT / "infrastructure/hosts/nas/access.yaml"
    rights = yaml.safe_load(access_path.read_text(encoding="utf-8"))
    assert not (set(rights) & {"users", "groups"})

    ready_catalog = copy.deepcopy(catalog)
    ready_catalog["users"] = [
        {"name": "sampleuser", "groups": ["family"]},
        {"name": "samplemanager", "groups": ["admins", "family"]},
    ]
    ready_rights = copy.deepcopy(rights)
    assert ready_rights["mode"] == "named_users"
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
    invalid["shares"]["music"]["access"] = "read_only"
    refuses(lambda: validate_nas_access(ROOT, invalid, ready_catalog))

    invalid = copy.deepcopy(ready_rights)
    del invalid["shares"]["music"]
    refuses(lambda: validate_nas_access(ROOT, invalid, ready_catalog))

    invalid = copy.deepcopy(ready_rights)
    invalid["shares"]["rogue"] = {"access": "read_write"}
    refuses(lambda: validate_nas_access(ROOT, invalid, ready_catalog))

    invalid = copy.deepcopy(ready_rights)
    invalid["mode"] = "guest_only"
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

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "password"
        local = {**merged, "credential": {"path": str(path)}}
        refuses(lambda: require_nas_smb_secret(local))
        path.write_text("test-password\n", encoding="utf-8")
        os.chmod(path, 0o644)
        refuses(lambda: require_nas_smb_secret(local))
        os.chmod(path, 0o600)
        require_nas_smb_secret(local)
        path.unlink()
        path.symlink_to(Path(td) / "target")
        refuses(lambda: require_nas_smb_secret(local))

    with tempfile.TemporaryDirectory() as td:
        state = Path(td)
        destination = state / "nas-smb-password"
        with patch("os.fchown") as chown:
            store_nas_smb_secret("example-123", directory=state)
            chown.assert_called_once()
        assert destination.stat().st_mode & 0o777 == 0o600
        assert destination.read_text(encoding="utf-8") == "example-123\\n"
        local = {**merged, "credential": {"path": str(destination)}}
        assert require_nas_smb_secret(local) == "example-123"
        with patch("os.fchown"):
            store_nas_smb_secret("next-secret", directory=state)
        assert require_nas_smb_secret(local) == "next-secret"
        refuses(lambda: store_nas_smb_secret("bad\\npass", directory=state))
        assert require_nas_smb_secret(local) == "next-secret"
        destination.unlink()
        destination.symlink_to(state / "another-file")
        refuses(lambda: store_nas_smb_secret("valid-pass", directory=state))

    print("[ОК] Samba named_users/read_write, пароль отдельно от Git, Authelia отдельно")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
