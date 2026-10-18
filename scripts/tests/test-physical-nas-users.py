#!/usr/bin/env python3
"""Проверки описания пользователей NAS без настоящих учётных записей."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_nas_users import (
    load_nas_users, require_nas_users_configured, validate_nas_users,
)


def refuses(callback) -> None:
    """Ожидать отказ для некорректного списка прав или пользователей."""

    try:
        callback()
    except InfraManagerError:
        return
    raise AssertionError("Ошибочная конфигурация пользователей NAS принята")


def main() -> int:
    """Проверить пустой безопасный шаблон и осмысленные заполненные списки."""

    empty = load_nas_users(ROOT)
    assert empty["schema_version"] == 1
    assert empty["users"] == []
    assert empty["groups"] == []
    assert set(empty["shares"]) == {"shared", "movies", "music", "data"}
    refuses(lambda: require_nas_users_configured(ROOT))

    ready = copy.deepcopy(empty)
    ready["groups"] = ["nas-family", "nas-managers"]
    ready["users"] = [
        {"name": "sampleuser", "groups": ["nas-family"]},
        {"name": "samplemanager", "groups": ["nas-family", "nas-managers"]},
    ]
    ready["shares"]["shared"]["write_groups"] = ["nas-family"]
    ready["shares"]["data"]["write_groups"] = ["nas-managers"]
    assert validate_nas_users(ROOT, ready) == ready

    invalid = copy.deepcopy(ready)
    invalid["users"][0]["password"] = "must-not-appear-in-git"
    refuses(lambda: validate_nas_users(ROOT, invalid))

    invalid = copy.deepcopy(ready)
    invalid["users"][0]["groups"] = ["unknown-group"]
    refuses(lambda: validate_nas_users(ROOT, invalid))

    invalid = copy.deepcopy(ready)
    invalid["users"][1]["name"] = "sampleuser"
    refuses(lambda: validate_nas_users(ROOT, invalid))

    invalid = copy.deepcopy(ready)
    invalid["shares"]["music"]["read_groups"] = ["unknown-group"]
    refuses(lambda: validate_nas_users(ROOT, invalid))

    invalid = copy.deepcopy(ready)
    invalid["users"][0]["name"] = "root"
    refuses(lambda: validate_nas_users(ROOT, invalid))

    invalid = copy.deepcopy(ready)
    invalid["groups"] = ["nas-family", "nas-family"]
    refuses(lambda: validate_nas_users(ROOT, invalid))

    print("[ОК] Список NAS: имена, группы, права и запрет паролей проверены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
