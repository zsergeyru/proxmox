#!/usr/bin/env python3
"""Безопасные проверки Debian/OMV и идентификации томов NAS."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
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

    path = ROOT / "automation/ansible/roles/nas/tasks/base.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, list) and len(document) > 10
    tasks = document
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
    packages = next(item for item in tasks if item.get("name") ==
        "Проверить недостающие пакеты ZFS и Docker")
    assert packages["register"] == "nas_required_packages"
    package_args = packages["ansible.builtin.command"]["argv"]
    assert "nas_provision.system.packages.omv_extensions" in package_args
    assert "nas_provision.system.packages.docker" in package_args
    assert "dpkg-query" in package_args and "binary:Package" in package_args
    assert r"\n" in package_args
    after_extras = next(item for item in tasks if item.get("name") ==
        "Обновить перечень пакетов после установки OMV-Extras")
    command = after_extras["ansible.builtin.command"]["argv"]
    assert command == ["apt-get", "-o", "APT::Update::Error-Mode=any", "update"]
    assert after_extras["failed_when"] is False
    assert after_extras["register"] == "nas_apt_refresh"
    assert "nas_required_packages.stdout_lines" in after_extras["when"]
    assert "!= 6" in after_extras["when"]
    fail = next(item for item in tasks if item.get("name") ==
        "Прервать настройку из-за ошибки обновления APT")
    assert "nas_apt_refresh.rc" in str(fail["when"])
    assert "не начиналось" in fail["ansible.builtin.fail"]["msg"]
    dns = next(item for item in tasks if item.get("name") ==
        "Проверить DNS основного репозитория OMV после ошибки APT")
    assert dns["ansible.builtin.command"]["argv"] == [
        "getent", "ahostsv4", "packages.openmediavault.org"
    ]
    assert dns["failed_when"] is False and dns["changed_when"] is False
    https = next(item for item in tasks if item.get("name") ==
        "Проверить HTTPS-ответ основного репозитория OMV после ошибки APT")
    curl = https["ansible.builtin.command"]["argv"]
    assert curl[0] == "curl"
    assert "--max-time" in curl and "--connect-timeout" in curl
    assert curl[-1] == "https://packages.openmediavault.org/public/dists/synchrony/InRelease"
    assert https["failed_when"] is False and https["changed_when"] is False
    assert "nas_apt_refresh.rc" in str(dns["when"])
    assert "nas_apt_refresh.rc" in str(https["when"])
    recovery = next(item for item in tasks if item.get("name") ==
        "Восстановить официальный список зеркал OMV при подтверждённом HTTP 503")
    assert recovery["ansible.builtin.script"]["cmd"].endswith(
        "nas-omv-mirror-recover.py apply"
    )
    assert "503" in str(recovery["when"])
    assert "packages.openmediavault.org" in str(recovery["when"])
    retry = next(item for item in tasks if item.get("name") ==
        "Повторить обновление APT через проверенные зеркала OMV")
    assert retry["register"] == "nas_apt_mirror_refresh"
    assert retry["failed_when"] is False
    rollback = next(item for item in tasks if item.get("name") ==
        "Откатить изменения OMV при повторной ошибке обновления APT")
    assert rollback["ansible.builtin.script"]["cmd"].endswith(
        "nas-omv-mirror-recover.py rollback"
    )
    assert "nas_apt_mirror_refresh.rc" in str(rollback["when"])
    assert "nas_apt_mirror_refresh" in str(fail["when"])


def test_zfs_dkms_stays_on_running_kernel_and_precedes_disk_changes() -> None:
    """При отсутствии модуля ставятся точные заголовки без смены ядра."""

    path = ROOT / "automation/ansible/roles/nas/tasks/base.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    def step(name: str):
        result = next(task for task in tasks if task.get("name") == name)
        return result, tasks.index(result)

    initial, first = step("Проверить наличие модуля ZFS после установки расширения")
    validate, _ = step("Проверить безопасное имя работающего ядра для ZFS")
    headers, header_index = step("Проверить заголовки работающего ядра для DKMS")
    apt, apt_index = step("Установить заголовки текущего ядра и DKMS без удаления пакетов")
    after_headers, _ = step("Проверить ZFS после установки заголовков")
    dkms, dkms_index = step("Собрать ZFS для работающего ядра через DKMS")
    ready, ready_index = step("Проверить модуль ZFS после DKMS")
    confirm, confirm_index = step("Подтвердить готовность ZFS после сборки DKMS")
    load, load_index = step("Загрузить проверенный модуль ZFS в работающее ядро")
    sys_module, sys_index = step("Проверить загрузку ZFS в ядро")
    loaded, loaded_index = step("Подтвердить загрузку ZFS в работающее ядро")
    docker, docker_index = step("Установить Docker через источник пакетов, управляемый OMV-Extras")
    storage, storage_index = step("Установить одноразовый исполнитель подготовки пустых HDD")
    assert first < header_index < apt_index < dkms_index < ready_index
    assert ready_index < confirm_index < load_index < sys_index < loaded_index
    assert loaded_index < docker_index < storage_index

    assert initial["ansible.builtin.command"]["argv"] == ["modinfo", "zfs"]
    assert "ansible_facts['kernel']" in str(validate["ansible.builtin.assert"]["that"])
    assert validate["when"] == "nas_zfs_module.rc != 0"
    assert headers["ansible.builtin.stat"]["follow"] is True
    assert "/lib/modules/{{ ansible_facts['kernel'] }}/build" == headers["ansible.builtin.stat"]["path"]
    assert headers["when"] == "nas_zfs_module.rc != 0"

    apt_package = apt["ansible.builtin.apt"]
    assert apt_package["name"] == [
        "linux-headers-{{ ansible_facts['kernel'] }}", "dkms"
    ]
    assert apt_package["state"] == "present"
    assert apt_package["fail_on_autoremove"] is True
    assert apt_package["force_apt_get"] is True
    assert apt_package["update_cache"] is False
    assert "nas_zfs_module.rc != 0" in apt["when"]
    assert "nas_zfs_kernel_headers.stat.isdir" in str(apt["when"])
    assert after_headers["failed_when"] is False
    assert "nas_zfs_module.rc != 0" in str(after_headers["when"])

    assert dkms["ansible.builtin.command"]["argv"] == [
        "dkms", "autoinstall", "-k", "{{ ansible_facts['kernel'] }}"
    ]
    assert dkms["failed_when"] is False
    assert "nas_zfs_module_after_headers.rc != 0" in dkms["when"]
    assert ready["ansible.builtin.command"]["argv"] == [
        "modinfo", "-F", "filename", "zfs"
    ]
    assert ready["failed_when"] is False
    assert "nas_zfs_ready.rc == 0" in confirm["ansible.builtin.assert"]["that"]
    assert "nas_zfs_dkms_build" in str(confirm["ansible.builtin.assert"]["that"])
    assert load["ansible.builtin.command"]["argv"] == ["modprobe", "zfs"]
    assert load["failed_when"] is False
    assert sys_module["ansible.builtin.stat"]["path"] == "/sys/module/zfs"
    assert "nas_zfs_modprobe.rc == 0" in loaded["ansible.builtin.assert"]["that"]
    assert "nas_zfs_loaded.stat.isdir" in str(loaded["ansible.builtin.assert"]["that"])

    body = str(tasks)
    assert "reboot" not in body.lower()
    assert "autoremove" not in body.lower() or "fail_on_autoremove" in body
    assert "linux-image-" not in str(apt_package["name"])


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
            "stdout": ("Err:1 https://alice:SECRET@example.org/path?token=SENSITIVE "
                       "Temporary failure resolving 'example.org'"),
            "stderr": "GPG error: NO_PUBKEY 1234abcd",
        },
    ))
    report = "\n".join(output)
    assert "APT update: код возврата 100" in report
    assert "DNS" in report
    assert "GPG" in report
    assert "[РЕПОЗИТОРИЙ] example.org" in report
    assert "alice" not in report
    assert "SECRET" not in report and "SENSITIVE" not in report
    assert "https://" not in report

    output.clear()
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Проверить DNS основного репозитория OMV после ошибки APT",
        ),
        _result={"rc": 2, "stdout": "SECRET_DNS_DETAILS"},
    ))
    assert "не удалось разрешить имя" in "\n".join(output)
    assert "SECRET_DNS_DETAILS" not in "\n".join(output)

    output.clear()
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Проверить HTTPS-ответ основного репозитория OMV после ошибки APT",
        ),
        _result={"rc": 0, "stdout": "403", "stderr": "SECRET_HEADER"},
    ))
    assert "HTTP 403" in "\n".join(output)
    assert "независимая проверка" in "\n".join(output)
    assert "SECRET_HEADER" not in "\n".join(output)

    output.clear()
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Проверить HTTPS-ответ основного репозитория OMV после ошибки APT",
        ),
        _result={"rc": 60, "stdout": "000", "stderr": "Authorization: SECRET_HEADER"},
    ))
    assert "HTTP-ответ не получен" in "\n".join(output)
    assert "сертификат TLS" in "\n".join(output)
    assert "SECRET_HEADER" not in "\n".join(output)

    output.clear()
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Собрать ZFS для работающего ядра через DKMS",
        ),
        _result={
            "rc": 10, "stdout": "Bad return status for module build",
            "stderr": "SECRET_COMPILATION_PATH",
        },
    ))
    assert "кодом 10" in "\n".join(output)
    assert "Компилятор" in "\n".join(output)
    assert "SECRET_COMPILATION_PATH" not in "\n".join(output)

    output.clear()
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Загрузить проверенный модуль ZFS в работающее ядро",
        ),
        _result={"rc": 1, "stderr": "SECRET_KERNEL_DATA"},
    ))
    assert "modprobe zfs" in "\n".join(output)
    assert "SECRET_KERNEL_DATA" not in "\n".join(output)

    output.clear()
    callback.v2_runner_on_ok(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Подтвердить загрузку ZFS в работающее ядро",
        ),
        _result={"changed": False},
    ))
    assert "ZFS загружен для работающего ядра NAS" in "\n".join(output)

    output.clear()
    callback.v2_runner_on_failed(SimpleNamespace(
        _task=SimpleNamespace(
            no_log=False,
            get_name=lambda: "Подтвердить готовность ZFS после сборки DKMS",
        ),
        _result={"msg": "SECRET_OTHER_DATA"},
    ))
    assert "ZFS не собран" in "\n".join(output)
    assert "SECRET_OTHER_DATA" not in "\n".join(output)

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


def test_omv_repository_fallback_guard_and_rollback() -> None:
    """Сохранять подписи и конфигурацию OMV, блокировать нестандартные источники."""

    source_file = ROOT / "automation/ansible/roles/nas/files/nas-omv-mirror-recover.py"
    spec = importlib.util.spec_from_file_location("nas_omv_mirror_recovery", source_file)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "apt/sources.list.d/openmediavault.list"
        mirrors = root / "apt/mirrors/openmediavault.txt"
        state = root / "state/omv-backup.json"
        keyring = root / "apt/keyrings/openmediavault.gpg"
        source.parent.mkdir(parents=True)
        keyring.parent.mkdir(parents=True)
        keyring.write_bytes(b"STUB KEY FILE")
        original = (
            "# Managed by OMV\n"
            "deb [signed-by=/usr/share/keyrings/openmediavault-archive-keyring.gpg] "
            "http://packages.openmediavault.org/public/ synchrony main\n"
        )
        source.write_text(original, encoding="utf-8")
        with (
            patch.object(script, "SOURCE", source),
            patch.object(script, "MIRRORS", mirrors),
            patch.object(script, "STATE", state),
            patch.object(script, "KEYRING", keyring),
        ):
            script._apply()
            assert script.MIRROR_SOURCE in source.read_text(encoding="utf-8")
            assert "openmediavault.github.io/packages/" in mirrors.read_text()
            assert "signed-by=" in source.read_text()
            assert state.is_file() and state.stat().st_mode & 0o777 == 0o600
            script._apply()  # безопасный повтор
            assert state.is_file()
            # Любое постороннее изменение файлов блокирует автоматический откат.
            mirrors.write_text("https://malicious.example.invalid/\n")
            try:
                script._rollback()
            except ValueError:
                pass
            else:
                raise AssertionError("Нельзя откатывать изменённый посторонним файл")
            mirrors.write_text(script.MIRROR_CONTENT, encoding="utf-8")
            script._rollback()
            assert source.read_text() == original
            assert not mirrors.exists() and not state.exists()
            # Нестандартные адреса, неподписанные репозитории и сторонние
            # источники категорически нельзя заменять автоматически.
            for unexpected in (
                "deb [trusted=yes] https://repo.example.invalid synchrony main\n",
                "deb https://openmediavault.github.io/packages/ synchrony main\n",
                "deb [signed-by=/tmp/other.gpg] "
                "https://packages.openmediavault.org/public synchrony main\n",
                original + "deb https://example.invalid trixie main\n",
            ):
                source.write_text(unexpected, encoding="utf-8")
                try:
                    script._apply()
                except ValueError:
                    pass
                else:
                    raise AssertionError("Нестандартный источник APT был заменён")
                assert source.read_text() == unexpected
                assert not state.exists()
            source.write_text(original)
            keyring.unlink()
            try:
                script._apply()
            except ValueError:
                pass
            else:
                raise AssertionError("Нельзя переключать зеркало без штатного ключа")
            assert source.read_text() == original


def main() -> int:
    """Запустить проверки без настоящего сервера."""

    test_system()
    test_paths()
    test_unconfirmed_volumes()
    test_nas_apt_diagnostic_guard()
    test_zfs_dkms_stays_on_running_kernel_and_precedes_disk_changes()
    test_nas_compact_callback_hides_untrusted_output()
    test_omv_repository_fallback_guard_and_rollback()
    print("[ОК] Безопасные проверки OMV и подключений пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
