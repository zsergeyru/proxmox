#!/usr/bin/env python3
"""Контракты общих папок OMV и безопасного локального резервирования ZFS."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(file: str, alias: str):
    """Загрузить локальный сценарий без установки на NAS."""

    spec = importlib.util.spec_from_file_location(
        alias, ROOT / "automation/ansible/files" / file
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"Не найден сценарий: {file}")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


shares = module("nas-omv-shares.py", "nas_omv_shares_test")
backup = module("nas-zfs-backup.py", "nas_zfs_backup_test")


def refusal(callback, error_type: type[Exception]) -> None:
    """Убедиться, что конфликт не изменяет чужие настройки."""

    try:
        callback()
    except error_type:
        return
    raise AssertionError("Опасная конфигурация была принята")


def test_shares() -> None:
    """Не выдавать гостевой доступ и не переиспользовать чужую папку."""

    folders, smb = shares.plan_shares("shared-ref", "data-ref", [], [])
    assert len(folders) == 4 and len(smb) == 4
    assert {folder["name"] for folder in folders} == {
        "shared", "movies", "music", "data"
    }
    assert all(item["guest"] == "no" for item in smb)
    assert all(item["enable"] is True for item in smb)
    existing = [{
        "uuid": "existing-uuid",
        "name": "shared",
        "mntentref": "wrong",
        "reldirpath": "shared/",
    }]
    refusal(
        lambda: shares.plan_shares("shared-ref", "data-ref", existing, []),
        shares.ShareError,
    )
    correct = [{
        "uuid": "existing-uuid",
        "name": "shared",
        "mntentref": "shared-ref",
        "reldirpath": "shared/",
    }]
    guests = [{
        "uuid": "smb-uuid", "sharedfolderref": "existing-uuid", "guest": "allow"
    }]
    refusal(
        lambda: shares.plan_shares("shared-ref", "data-ref", correct, guests),
        shares.ShareError,
    )


def test_backup_identifiers() -> None:
    """Основной пул никогда не должен стать собственным приёмником."""

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "backup.json"
        with patch.object(backup, "CONFIG", path):
            def write(data: dict[str, str]) -> None:
                path.write_text(json.dumps(data), encoding="utf-8")

            good = {
                "source_pool": "nas-data", "source_guid": "123456",
                "backup_pool": "nas-backup", "backup_guid": "654321",
            }
            write(good)
            assert backup.load_configuration() == good
            write({**good, "backup_guid": "123456"})
            refusal(backup.load_configuration, backup.BackupError)
            write({**good, "source_pool": "nas-backup"})
            refusal(backup.load_configuration, backup.BackupError)


def test_no_forced_rollback() -> None:
    """Сценарий не имеет способов автоматического отката и удаления ZFS."""

    source = (
        ROOT / "automation/ansible/files/nas-zfs-backup.py"
    ).read_text(encoding="utf-8")
    for forbidden in ('"destroy"', '"-F"', '"rollback"', '"wipefs"'):
        assert forbidden not in source, forbidden
    with patch.object(backup, "execute") as execute:
        execute.side_effect = backup.BackupError("Пул не соответствует")
        refusal(lambda: backup.verify_pool("nas-data", "123"), backup.BackupError)


def main() -> int:
    """Проверить работу без реальных файлов пользователей."""

    test_shares()
    test_backup_identifiers()
    test_no_forced_rollback()
    print("[ОК] Общие папки и резервирование ZFS проверены на тестовых данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
