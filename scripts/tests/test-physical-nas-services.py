#!/usr/bin/env python3
"""Контракты общих папок OMV и безопасного локального резервирования ZFS."""

from __future__ import annotations

import importlib.util
import json
import yaml
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(file: str, alias: str):
    """Загрузить локальный сценарий без установки на NAS."""

    spec = importlib.util.spec_from_file_location(
        alias, ROOT / ("automation/ansible/files" if file == "nas-zfs-backup.py" else "automation/ansible/roles/nas/files") / file
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"Не найден сценарий: {file}")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


backup = module("nas-zfs-backup.py", "nas_zfs_backup_test")
def refusal(callback, error_type: type[Exception]) -> None:
    """Убедиться, что конфликт не изменяет чужие настройки."""

    try:
        callback()
    except error_type:
        return
    raise AssertionError("Опасная конфигурация была принята")


def test_omv_shares_are_declared_in_ansible() -> None:
    """Old Python OMV writer is retired, access comes from YAML and Ansible."""
    folder = ROOT / "automation/ansible/roles/nas"
    assert not (folder / "files/nas-omv-shares.py").exists()
    smb = yaml.safe_load((folder / "tasks/smb.yml").read_text(encoding="utf-8"))
    omv = yaml.safe_load(
        (folder / "tasks/omv-shares.yml").read_text(encoding="utf-8")
    )
    assert any(task.get("ansible.builtin.include_tasks") == "omv-shares.yml"
               for task in smb)
    assert all("ansible.builtin.script" not in task for task in omv)
    assert "nas_provision.shares" in str(omv)
    assert "nas_access_policy" in str(smb)
    assert "mntentref" in str(omv)
    assert "omv-confdbadm" in str(omv)
    assert "omv-salt" in str(omv)
    assert "readonly': false" in str(omv)
    assert "guest': 'no'" in str(omv)


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
        ROOT / "automation/ansible/roles/nas/tasks/apps.yml"
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
        ROOT / "automation/ansible/roles/nas/templates/nas-qbittorrent.service.j2"
    ).read_text(encoding="utf-8")
    assert "ExecStartPre=/usr/bin/findmnt" in unit
    assert "ConditionPathIsMountPoint=" in unit


def test_samba_service_automation() -> None:
    """Samba запускается штатной OMV конфигурацией, без правки smb.conf."""

    file = ROOT / "automation/ansible/roles/nas/tasks/smb.yml"
    content = file.read_text(encoding="utf-8")
    assert content.index("Согласовать объекты OMV штатными средствами Ansible") < (
        content.index("Включить автозапуск и запустить Samba")
    )
    assert "state: started" in content
    assert "enabled: true" in content
    assert "argv: [testparm, -s]" in content
    assert "Прочитать журнал отказа Samba" in content
    assert "Прервать настройку при ошибке службы Samba" in content
    assert "/etc/samba/smb.conf" not in content
    assert "smbpasswd, -s, -a" in content
    assert "no_log: true" in content
    assert "password_lock: true" in content
    assert "append: true" in content
    assert "lookup('ansible.builtin.file'" in content
    assert "nas_access_policy.mode == 'named_users'" in content
    assert "g:nas-smb:rwx,m::rwx,d:g:nas-smb:rwx,d:m::rwx" in content
    assert content.index("Назначить группе Samba полные права") < (
        content.index("Согласовать объекты OMV штатными средствами Ansible")
    )
    smb_tasks = yaml.safe_load(content)
    names = [t["name"] for t in smb_tasks]
    def order(name: str) -> int:
        return names.index(name)
    assert order("Повторно подтвердить GUID data ZFS перед настройкой прав Samba") < (
        order("Включить POSIX ACL существующего ZFS без форматирования")
    )
    assert order("Включить POSIX ACL существующего ZFS без форматирования") < (
        order("Создать объявленные общие каталоги без удаления файлов")
    )
    assert order("Запретить публикацию Samba без действующих POSIX ACL") < (
        order("Назначить группе Samba полные права и наследование для новых файлов")
    )
    acl_cmds = [t for t in smb_tasks if t.get("ansible.builtin.command", {}).get(
        "argv", [None])[:2] == ["zfs", "set"]]
    assert len(acl_cmds) == 1
    assert acl_cmds[0]["ansible.builtin.command"]["argv"] == [
        "zfs", "set", "acltype=posixacl", "{{ nas_data_pool }}"
    ]
    assert "nas_smb_zfs_acl_before.stdout" in acl_cmds[0]["when"]
    assert "nas_smb_zfs_xattr.stdout" in content
    assert "['dir', 'sa', 'on']" in content


def test_samba_safe_error_hints() -> None:
    """Выводим только категории ошибок из фиксированного словаря."""

    ansible = ModuleType("ansible")
    plugins = ModuleType("ansible.plugins")
    callback_module = ModuleType("ansible.plugins.callback")
    callback_module.CallbackBase = object
    path = ROOT / "automation/ansible/callback_plugins/infra_nas_compact.py"
    spec = importlib.util.spec_from_file_location("test_samba_callback", path)
    assert spec is not None and spec.loader is not None
    module_obj = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "ansible": ansible,
        "ansible.plugins": plugins,
        "ansible.plugins.callback": callback_module,
    }):
        spec.loader.exec_module(module_obj)

    reasons = module_obj.CallbackModule._samba_reasons
    for message, expected in (
        ("bind: Address already in use on port 445", "Порт Samba"),
        ("failed to load services from smb.conf", "конфигурации"),
        ("Unit smbd.service is masked", "заблокирована"),
    ):
        classifications = reasons(message + " confidential-value-1234")
        assert any(expected in item for item in classifications)
        assert all("confidential-value-1234" not in item for item in classifications)
    assert "confidential-value-1234" not in " ".join(
        reasons("confidential-value-1234")
    )


def main() -> int:
    """Проверить работу без реальных файлов пользователей."""

    test_omv_shares_are_declared_in_ansible()
    test_samba_service_automation()
    test_samba_safe_error_hints()
    test_backup_identifiers()
    test_no_forced_rollback()
    test_qbittorrent_failure_diagnostics()
    test_qbittorrent_deploy_prepares_image_safely()
    print("[ОК] Общие папки и резервирование ZFS проверены на тестовых данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
