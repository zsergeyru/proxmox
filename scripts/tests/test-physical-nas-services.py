#!/usr/bin/env python3
"""Контракты общих папок OMV и безопасного локального резервирования ZFS."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType
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



def test_qbittorrent_failure_diagnostics() -> None:
    """Причины Docker/systemd классифицируются, секреты не выводятся."""

    # Проверка callback должна работать и без установки Ansible на машине.
    ansible = ModuleType("ansible")
    plugins = ModuleType("ansible.plugins")
    base = ModuleType("ansible.plugins.callback")
    base.CallbackBase = object
    path = ROOT / "automation/ansible/callback_plugins/infra_nas_compact.py"
    spec = importlib.util.spec_from_file_location("nas_test_callback", path)
    assert spec is not None and spec.loader is not None
    callback = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "ansible": ansible,
        "ansible.plugins": plugins,
        "ansible.plugins.callback": base,
    }):
        spec.loader.exec_module(callback)

    classify = callback.CallbackModule._qbittorrent_reasons
    for log, hint in (
        ("Error: port is already allocated", "сетевой порт"),
        ("manifest unknown: test", "недоступны в реестре"),
        ("Error response: no space left on device", "недостаточно"),
        ("lookup lscr.io: no such host", "DNS"),
        ("Error: Cannot connect to the Docker daemon", "Docker Engine"),
    ):
        result = classify(log + " password=DO_NOT_LEAK_SECRET")
        assert any(hint in item for item in result), (log, result)
        assert all("DO_NOT_LEAK_SECRET" not in item for item in result)
        assert all("lscr.io" not in item for item in result)
    unknown = classify("token=DO_NOT_LEAK_SECRET")
    assert len(unknown) == 1
    assert "DO_NOT_LEAK_SECRET" not in unknown[0]


def test_qbittorrent_deploy_prepares_image_safely() -> None:
    """Не скачивать контейнер до проверки HDD, а при отказе читать журнал."""

    playbook = (
        ROOT / "automation/ansible/playbooks/configure-nas-apps.yml"
    ).read_text(encoding="utf-8")
    assert playbook.index("Повторно сверить точку подключения и UUID ext4") < (
        playbook.index("Загрузить образ qBittorrent при отсутствии")
    )
    assert playbook.index("Загрузить образ qBittorrent при отсутствии") < (
        playbook.index("Подключить службу qBittorrent после проверки HDD")
    )
    assert "when: nas_qbit_image_present.rc != 0" in playbook
    assert "until: nas_qbit_image_pull.rc == 0" in playbook
    assert "Прочитать журнал отказа запуска qBittorrent" in playbook
    assert "failed_when: false" in playbook
    assert "wipefs" not in playbook and "mkfs" not in playbook
    unit = (
        ROOT / "automation/ansible/templates/nas-qbittorrent.service.j2"
    ).read_text(encoding="utf-8")
    assert "ExecStartPre=/usr/bin/findmnt" in unit
    assert "ConditionPathIsMountPoint=" in unit


def main() -> int:
    """Проверить работу без реальных файлов пользователей."""

    test_shares()
    test_backup_identifiers()
    test_no_forced_rollback()
    test_qbittorrent_failure_diagnostics()
    test_qbittorrent_deploy_prepares_image_safely()
    print("[ОК] Общие папки и резервирование ZFS проверены на тестовых данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
