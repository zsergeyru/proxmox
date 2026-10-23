#!/usr/bin/env python3
"""Безопасные проверки Debian/OMV и идентификации томов NAS."""

from __future__ import annotations

import sys
import yaml
from pathlib import Path
from types import SimpleNamespace
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


def main() -> int:
    """Запустить проверки без настоящего сервера."""

    test_system()
    test_paths()
    test_unconfirmed_volumes()
    test_nas_apt_diagnostic_guard()
    print("[ОК] Безопасные проверки OMV и подключений пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
