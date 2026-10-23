#!/usr/bin/env python3
"""Безопасные проверки Debian/OMV и идентификации томов NAS."""

from __future__ import annotations

import importlib.util
import sys
import yaml
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_nas_system import (
    NasSystem, parse_os_release, parse_omv_mounts, require_omv8,
)
from infra_manager.physical_nas_volumes import (
    _configured_path, _parse_findmnt, verify_nas_volumes,
)
from infra_manager.physical_hosts import load_physical_host


def refuses(callback) -> None:
    """Ожидать остановку без каких-либо удалённых изменений."""

    try:
        callback()
    except InfraManagerError:
        return
    raise AssertionError("Опасная конфигурация была принята")


def test_system() -> None:
    """Устанавливать ОС запрещено; требовать Debian 13 / OMV 8."""

    assert parse_os_release('ID=debian\nVERSION_ID="13"\n') == "13"
    refuses(lambda: parse_os_release('ID=debian\nVERSION_ID="12"\n'))
    refuses(lambda: parse_os_release('ID=ubuntu\nVERSION_ID="13"\n'))
    state = NasSystem("13", "8.1.0-2", "amd64", ())
    require_omv8(state)
    refuses(lambda: require_omv8(NasSystem("13", "7.9.0", "amd64", ())))
    assert parse_omv_mounts("[]") == ()
    refuses(lambda: parse_omv_mounts("not JSON"))
    refuses(lambda: parse_omv_mounts(
        '[{"uuid":"same","dir":"/srv/a"},{"uuid":"same","dir":"/srv/b"}]'
    ))


def test_paths() -> None:
    """Проверить отказ от SSD, родительского раздела и обхода каталога."""

    for candidate in (None, "/", "/srv/../etc", "/etc", "/var/tmp",
                      "srv/shared", "/srv//shared", "/root/private"):
        refuses(lambda value=candidate: _configured_path(value))
    assert _configured_path("/srv/dev-disk-by-uuid-abc") == "/srv/dev-disk-by-uuid-abc"
    refuses(lambda: _parse_findmnt(
        '{"filesystems":[{"target":"/","source":"/dev/sda","fstype":"ext4"}]}',
        "/srv/data",
    ))


def test_unconfirmed_volumes() -> None:
    """Не выполнять даже регистрацию папок до настройки устойчивых UUID."""

    nas = load_physical_host(ROOT, "nas")
    system = NasSystem("13", "8.0", "amd64", ())
    with patch(
        "infra_manager.physical_nas_volumes.run",
        return_value=SimpleNamespace(returncode=0, stdout="{}"),
    ) as remote:
        refuses(lambda: verify_nas_volumes(nas, system, SimpleNamespace()))
        remote.assert_not_called()


def test_nas_apt_diagnostic_guard() -> None:
    """Ошибка обновления APT должна быть отличима от форматирования HDD."""

    path = ROOT / "automation/ansible/playbooks/configure-nas.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, list) and len(document) == 1
    tasks = document[0]["tasks"]
    package_step = next(item for item in tasks if item.get("name") ==
        "Установить средства диагностики, не изменяя разметку")
    install = package_step["block"][0]["ansible.builtin.apt"]
    assert install["state"] == "present"
    assert install["update_cache"] is False
    assert any("getent" in str(entry) for entry in package_step["rescue"])
    assert "На HDD ничего не форматировалось" in str(package_step["rescue"])
    ansible = (ROOT / "scripts/infra-manager/infra_manager/physical_nas_ansible.py"
              ).read_text(encoding="utf-8")
    assert '"ANSIBLE_LOG_PATH": "/dev/null"' in ansible
    assert '"ANSIBLE_SSH_TRANSFER_METHOD": "piped"' in ansible
    assert '"ANSIBLE_STDOUT_CALLBACK": "infra_nas_compact"' in ansible
    assert '"ANSIBLE_CALLBACK_PLUGINS": str(' in ansible
    assert '"ansible_python_interpreter": "/usr/bin/python3"' in ansible
    after_extras = next(item for item in tasks if item.get("name") ==
        "Обновить перечень пакетов после установки OMV-Extras")
    command = after_extras["ansible.builtin.command"]["argv"]
    assert command == ["apt-get", "-o", "APT::Update::Error-Mode=any", "update"]
    assert after_extras["failed_when"] is False
    assert after_extras["register"] == "nas_apt_refresh"
    fail = next(item for item in tasks if item.get("name") ==
        "Прервать настройку из-за ошибки обновления APT")
    assert "nas_apt_refresh.rc" in str(fail["when"])
    assert "не начиналось" in fail["ansible.builtin.fail"]["msg"]


def test_nas_compact_callback_hides_untrusted_output() -> None:
    """Показывать причину APT, а не пароль/URL из сырых stdout/stderr."""

    source = ROOT / "automation/ansible/callback_plugins/infra_nas_compact.py"
    spec = importlib.util.spec_from_file_location("infra_nas_compact", source)
    assert spec and spec.loader
    # Этот контрактный тест работает и без системного Ansible: CI запускает
    # его раньше сборки runtime-контейнера с реальным ansible-core.
    fake_ansible = ModuleType("ansible")
    fake_plugins = ModuleType("ansible.plugins")
    fake_callback = ModuleType("ansible.plugins.callback")
    fake_callback.CallbackBase = type("CallbackBase", (), {
        "__init__": lambda self: setattr(
            self, "_display", SimpleNamespace(display=lambda message: None)
        ),
    })
    plugin = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "ansible": fake_ansible,
        "ansible.plugins": fake_plugins,
        "ansible.plugins.callback": fake_callback,
    }):
        spec.loader.exec_module(plugin)
    callback = plugin.CallbackModule()
    output = []
    callback._display = SimpleNamespace(display=output.append)

    task = SimpleNamespace(
        no_log=False,
        get_name=lambda: "Обновить перечень пакетов после установки OMV-Extras",
    )
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=task, _result={
            "rc": 100,
            "stdout": ("Err: https://alice:SECRET@example.org/path?token=SENSITIVE "
                       "Temporary failure resolving 'example.org'"),
            "stderr": "GPG error: NO_PUBKEY 1234abcd",
        },
    ))
    report = "\n".join(output)
    assert "APT update: код возврата 100" in report
    assert "DNS" in report
    assert "GPG" in report
    assert "SECRET" not in report and "SENSITIVE" not in report
    assert "https://" not in report

    output.clear()
    callback.v2_runner_on_failed(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Прервать настройку из-за ошибки обновления APT",
        ),
        _result={"msg": "Authorization: SECRET_TOKEN in apt update"},
    ))
    assert "SECRET_TOKEN" not in "\n".join(output)
    assert "Прервать настройку из-за ошибки обновления APT" in "\n".join(output)

    output.clear()
    callback.v2_runner_on_failed(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=True,
            get_name=lambda: "Установить секрет",
        ),
        _result={"msg": "private-password-value"},
    ))
    assert "private-password-value" not in "\n".join(output)
    assert "скрыты" in "\n".join(output)


def main() -> int:
    """Запустить проверки без настоящего сервера."""

    test_system()
    test_paths()
    test_unconfirmed_volumes()
    test_nas_apt_diagnostic_guard()
    test_nas_compact_callback_hides_untrusted_output()
    print("[ОК] Безопасные проверки OMV и подключений пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
