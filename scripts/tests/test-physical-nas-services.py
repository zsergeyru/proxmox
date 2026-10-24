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


shares = module("nas-omv-shares.py", "nas_omv_shares_test")
DECLARED_SHARES = yaml.safe_load(
    (ROOT / "infrastructure/hosts/nas/provision.yaml").read_text()
)["shares"]
backup = module("nas-zfs-backup.py", "nas_zfs_backup_test")
ACCESS = yaml.safe_load(
    (ROOT / "infrastructure/hosts/nas/access.yaml").read_text()
)
ACCESS_POLICY = {"mode": ACCESS["mode"], "shares": ACCESS["shares"]}


def refusal(callback, error_type: type[Exception]) -> None:
    """Убедиться, что конфликт не изменяет чужие настройки."""

    try:
        callback()
    except error_type:
        return
    raise AssertionError("Опасная конфигурация была принята")


def test_shares() -> None:
    """Only explicitly declared authenticated named-user shares can be published."""
    plan = shares.validated_folders(DECLARED_SHARES)
    assert len(plan) == 4
    refusal(lambda: shares.validated_folders([
        {"name": "../etc", "volume": "data", "directory": "etc"}
    ]), shares.ShareError)
    refusal(lambda: shares.validated_folders([
        {"name": "x", "volume": "backup", "directory": "x"}
    ]), shares.ShareError)
    refusal(lambda: shares.validated_folders([
        {"name": "x", "volume": "data", "directory": "x"}
    ] * 2), shares.ShareError)
    for invalid in (
        {"mode": "guest_only", "shares": ACCESS_POLICY["shares"]},
        {"mode": "named_users", "shares": {"shared": {"access": "read_write"}}},
        {"mode": "named_users", "shares": {
            **ACCESS_POLICY["shares"], "backup": {"access": "read_write"}
        }},
        {"mode": "named_users", "shares": {
            **ACCESS_POLICY["shares"], "shared": {"access": "read_only"}
        }},
    ):
        refusal(lambda invalid=invalid: shares.validated_access(invalid, plan), shares.ShareError)
    folders, smb = shares.plan_shares(
        "shared-ref", "data-ref", [], [], plan, ACCESS_POLICY,
    )
    assert len(folders) == 4 and len(smb) == 4
    assert {folder["name"] for folder in folders} == {
        "shared", "movies", "music", "data"
    }
    assert all(item["guest"] == "no" and item["readonly"] is False for item in smb)
    assert all(item["enable"] is True for item in smb)
    foreign = [{
        "uuid": "existing-uuid", "name": "shared",
        "mntentref": "wrong", "reldirpath": "shared/",
    }]
    refusal(
        lambda: shares.plan_shares(
            "shared-ref", "data-ref", foreign, [], plan, ACCESS_POLICY,
        ), shares.ShareError,
    )
    correct = [{
        "uuid": "00000000-0000-4000-8000-000000000001",
        "name": "shared", "mntentref": "shared-ref", "reldirpath": "shared/",
    }]
    old = [{
        "uuid": "00000000-0000-4000-9000-000000000001",
        "sharedfolderref": correct[0]["uuid"],
        "guest": "only", "readonly": True, "enable": False,
        "comment": "preserve",
    }]
    _folders, desired = shares.plan_shares(
        "shared-ref", "data-ref", correct, old, plan, ACCESS_POLICY,
    )
    adjusted = next(x for x in desired if x.get("uuid") == old[0]["uuid"])
    assert adjusted["guest"] == "no"
    assert adjusted["readonly"] is False
    assert adjusted["enable"] is True
    assert adjusted["comment"] == "preserve"


def test_omv_shared_folders_use_real_database_uuids() -> None:
    """OMV выдаёт UUID новых папок; SMB-ссылки используют только эти UUID."""

    folders: list[dict[str, object]] = []
    resources: list[dict[str, object]] = []
    settings: dict[str, object] = {"enable": False, "shares": {"share": []}}
    updates: list[tuple[str, dict[str, object]]] = []
    salt_deploys: list[int] = []

    def fake_command(*args: str) -> str:
        assert args[0] in ("omv-confdbadm", "omv-salt")
        if args[0] == "omv-salt":
            assert args == ("omv-salt", "deploy", "run", "samba")
            salt_deploys.append(1)
            return ""
        operation, model = args[1:3]
        if operation == "read":
            if model == "conf.service.smb":
                settings["shares"] = {"share": list(resources)}
            data = {
                "conf.system.sharedfolder": folders,
                "conf.service.smb.share": resources,
                "conf.service.smb": settings,
            }[model]
            return json.dumps(data)
        assert operation == "update"
        payload = json.loads(args[3])
        updates.append((model, payload))
        if model == "conf.system.sharedfolder":
            assert "uuid" not in payload
            assert payload["privileges"] == {"privilege": []}
            record = {**payload, "uuid": f"00000000-0000-4000-8000-{len(folders)+1:012x}"}
            folders.append(record)
        elif model == "conf.service.smb.share":
            assert payload["sharedfolderref"] in {item["uuid"] for item in folders}
            assert payload["guest"] == "no" and payload["readonly"] is False
            if "uuid" in payload:
                match = [item for item in resources if item["uuid"] == payload["uuid"]]
                assert len(match) == 1
                match[0].update(payload)
            else:
                resources.append({
                    **payload,
                    "uuid": f"00000000-0000-4000-9000-{len(resources)+1:012x}",
                })
        else:
            assert model == "conf.service.smb"
            resources[:] = list(payload.get("shares", {}).get("share", []))
            settings.clear()
            settings.update(payload)
        return ""

    with (
        patch.object(shares, "command", side_effect=fake_command),
        patch.object(shares.Path, "is_mount", return_value=True),
        patch.object(shares.Path, "is_dir", return_value=True),
        patch.object(shares.Path, "is_symlink", return_value=False),
    ):
        shares.apply("shared-ref", "data-ref", "/mnt/shared", "/mnt/data", DECLARED_SHARES, ACCESS_POLICY)
        assert len(folders) == 4 and len(resources) == 4
        assert len(salt_deploys) == 1
        assert settings["enable"] is True
        assert [entry["name"] for entry in folders] == [
            "shared", "movies", "music", "data",
        ]
        assert {entry["sharedfolderref"] for entry in resources} == {
            entry["uuid"] for entry in folders
        }
        # Converge a pre-existing authenticated share without removing its UUID.
        resources[0]["guest"] = "only"
        resources[0]["readonly"] = True
        original_uuid = resources[0]["uuid"]
        shares.apply("shared-ref", "data-ref", "/mnt/shared", "/mnt/data", DECLARED_SHARES, ACCESS_POLICY)
        assert resources[0]["uuid"] == original_uuid
        assert resources[0]["guest"] == "no" and resources[0]["readonly"] is False
        assert len(salt_deploys) == 2
        writes = len(updates)
        shares.apply("shared-ref", "data-ref", "/mnt/shared", "/mnt/data", DECLARED_SHARES, ACCESS_POLICY)
        assert len(updates) == writes
        assert len(salt_deploys) == 2
        # Частично записанное состояние при повторе не создаёт чужие записи.
        resources.pop()
        shares.apply("shared-ref", "data-ref", "/mnt/shared", "/mnt/data", DECLARED_SHARES, ACCESS_POLICY)
        assert len(resources) == 4
        assert len(salt_deploys) == 3
        # Включение Samba не стирает уже зарегистрированные SMB-ресурсы.
        settings["enable"] = False
        shares.apply("shared-ref", "data-ref", "/mnt/shared", "/mnt/data", DECLARED_SHARES, ACCESS_POLICY)
        assert len(resources) == 4
        assert len(salt_deploys) == 4


def test_omv_cli_failure_never_exposes_stderr() -> None:
    """Код ошибки содержит лишь этап и числовой rc, не содержимое OMV."""

    from types import SimpleNamespace

    result = SimpleNamespace(
        returncode=1, stdout="", stderr="SECRET_SHOULD_NOT_APPEAR"
    )
    with patch.object(shares.subprocess, "run", return_value=result):
        try:
            shares.command(
                "omv-confdbadm", "update", "conf.system.sharedfolder", "{}"
            )
        except shares.ShareError as exc:
            assert "NAS_SMB_STEP=create-folder RC=1" == str(exc)
            assert "SECRET_SHOULD_NOT_APPEAR" not in str(exc)
        else:
            raise AssertionError("OMV command failure was silently accepted")


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
    assert content.index("Зарегистрировать папки и ресурсы через базу OMV") < (
        content.index("Включить автозапуск и запустить Samba")
    )
    assert "state: started" in content
    assert "enabled: true" in content
    assert "samba-tool, testparm, --suppress-prompt" in content
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
        content.index("Зарегистрировать папки и ресурсы через базу OMV")
    )


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

    test_shares()
    test_omv_shared_folders_use_real_database_uuids()
    test_omv_cli_failure_never_exposes_stderr()
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
