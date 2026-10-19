#!/usr/bin/env python3
"""Автономные проверки первого SSH-доверия, без сетевых подключений и паролей."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))
from infra_manager import operator, physical_host_bootstrap as bootstrap
from infra_manager.common import InfraManagerError
from infra_manager.physical_hosts import load_physical_host

sys.path.insert(0, str(ROOT / "scripts/infra-manager/host"))
import infra_manager_nas_bootstrap as password_prompt

host_path = ROOT / "scripts/infra-manager/host/manager.py"
spec = importlib.util.spec_from_file_location("nas_host_manager", host_path)
if spec is None or spec.loader is None:
    raise SystemExit("Не найден оператор PVE")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def expect_refusal(callback) -> None:
    """Проверить ожидаемый безопасный отказ."""

    try:
        callback()
    except InfraManagerError:
        return
    raise AssertionError("Ожидался отказ начального доступа")


def test_marker() -> None:
    """Пароль доступен только до первой успешной отметки; смена адреса блокирует."""

    machine = replace(
        load_physical_host(ROOT, "nas"),
        address="192.0.2.22",
        ssh_host_fingerprint="SHA256:" + "B" * 43,
    )
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(bootstrap, "_MARKER_DIR", Path(directory)):
            assert not bootstrap.is_bootstrapped(machine)
            bootstrap._save_bootstrapped(machine)
            assert bootstrap.is_bootstrapped(machine)
            expect_refusal(lambda: bootstrap.is_bootstrapped(
                replace(machine, address="192.0.2.23")
            ))
            assert "password" not in bootstrap.marker_path("nas").read_text()


def test_operator_state() -> None:
    """Не запрашивать пароль после установки доверия; не принимать чужую отметку."""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "infrastructure/hosts/nas"
        target.mkdir(parents=True)
        target.joinpath("host.yaml").write_text(
            "connection:\n  address: 192.0.2.22\n"
            "  ssh_host_fingerprint: SHA256:" + "B" * 43 + "\n",
            encoding="utf-8",
        )
        paths = SimpleNamespace(repo_root=root, data_dir=root / "state")
        with patch.object(operator, "PATHS", paths):
            assert operator.operator_physical_bootstrap_state("nas") == 20
            marker = paths.data_dir / "locks/physical-hosts/nas.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                json.dumps({"name": "nas", "address": "192.0.2.22",
                            "fingerprint": "SHA256:" + "B" * 43}),
                encoding="utf-8",
            )
            assert operator.operator_physical_bootstrap_state("nas") == 0
            marker.write_text('{"name":"nas","address":"192.0.2.23"}')
            expect_refusal(lambda: operator.operator_physical_bootstrap_state("nas"))


def test_pve_password_transport() -> None:
    """Пароль не должен попадать в аргументы дочерних команд."""

    calls: list[tuple[list[str], object]] = []

    def forward(command: list[str], *, input_text: str | None = None) -> int:
        """Перехватить команды на LXC без запуска PVE."""

        calls.append((command, input_text))
        if command == ["bootstrap-state", "nas"]:
            return 20
        return 0

    with (
        patch.object(password_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", return_value="sergey"),
        patch.object(password_prompt.getpass, "getpass", return_value="TEST-NOT-REAL-SECRET"),
    ):
        assert password_prompt.initial_nas_access("nas", forward, host.OperatorError) == 0
    assert calls[0] == (["bootstrap-state", "nas"], None)
    assert calls[1][0] == ["bootstrap", "nas"]
    assert json.loads(calls[1][1]) == {
        "username": "sergey", "password": "TEST-NOT-REAL-SECRET",
    }
    assert all("TEST-NOT-REAL-SECRET" not in str(command) for command, _ in calls)

    calls.clear()
    with patch.object(password_prompt.getpass, "getpass") as prompt:
        def already_done(command: list[str], *, input_text: str | None = None) -> int:
            calls.append((command, input_text))
            return 0
        assert password_prompt.initial_nas_access("nas", already_done, host.OperatorError) == 0
        prompt.assert_not_called()


def main() -> int:
    """Проверить контракт до испытаний на реальном NAS."""

    test_marker()
    test_operator_state()
    test_pve_password_transport()
    print("[ОК] Первый SSH-доступ NAS проверен на испытательных данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
