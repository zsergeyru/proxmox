#!/usr/bin/env python3
"""Проверки автоматического восстановления системной зависимости 910 через PVE.

Не обращаются к реальному PVE и не устанавливают пакеты.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/infra-manager/host/manager.py"
spec = importlib.util.spec_from_file_location("test_pve_operator_deps_module", SOURCE)
assert spec is not None and spec.loader is not None
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)


def reply(code: int = 0, stderr: str = ""):
    return SimpleNamespace(returncode=code, stdout="", stderr=stderr)


def test_already_installed():
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return reply()

    with patch.object(manager, "run", side_effect=run):
        manager.restore_manager_python_dependency(910)
    assert commands == [
        ["pct", "exec", "910", "--", "python3", "-c", "import jsonschema"]
    ]


def test_missing_package_installed_automatically():
    commands = []
    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        if len(commands) == 1:
            return reply(1, "ModuleNotFoundError: No module named 'jsonschema'")
        return reply()

    with patch.object(manager, "run", side_effect=run):
        manager.restore_manager_python_dependency(910)
    assert len(commands) == 4
    assert commands[1][0] == ["pct", "exec", "910", "--", "apt-get", "update"]
    assert commands[2][0] == [
        "pct", "exec", "910", "--", "env", "DEBIAN_FRONTEND=noninteractive",
        "apt-get", "install", "-y", "python3-jsonschema",
    ]
    assert commands[3][0] == commands[0][0]


def test_unexpected_python_error_does_not_install():
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        return reply(1, "ImportError: unexpected Python failure")

    with patch.object(manager, "run", side_effect=run):
        try:
            manager.restore_manager_python_dependency(910)
        except manager.OperatorError:
            pass
        else:
            raise AssertionError("Непроверенная ошибка Python принята")
    assert len(commands) == 1


def test_preflight_only_for_verified_manager_deploy():
    with (
        patch.object(manager, "manager_identity", return_value=(910, "infra-manager")),
        patch.object(manager, "verify_manager"),
        patch.object(manager, "guest_running", return_value=True),
        patch.object(manager, "restore_manager_python_dependency") as bootstrap,
        patch.object(manager, "run", return_value=reply()) as invoke,
    ):
        assert manager.proxy_to_manager(["status", "910"]) == 0
        bootstrap.assert_not_called()
        assert manager.proxy_to_manager(["deploy", "410"]) == 0
        bootstrap.assert_not_called()
        assert manager.proxy_to_manager(["deploy", "910"]) == 0
        bootstrap.assert_called_once_with(910)
        assert any("pct" in call.args[0] for call in invoke.call_args_list)


def main():
    test_already_installed()
    test_missing_package_installed_automatically()
    test_unexpected_python_error_does_not_install()
    test_preflight_only_for_verified_manager_deploy()
    print("[ОК] PVE восстанавливает недостающую зависимость без ручного входа в гостя")


if __name__ == "__main__":
    main()
