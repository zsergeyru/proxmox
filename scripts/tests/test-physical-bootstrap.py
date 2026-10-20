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
from infra_manager import physical_host_connection as connection
from infra_manager.common import InfraManagerError
from infra_manager.physical_hosts import load_physical_host

sys.path.insert(0, str(ROOT / "scripts/infra-manager/host"))
import infra_manager_nas_bootstrap as password_prompt
import infra_manager_nas_connection as connection_prompt

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
        (Path(directory) / "physical-hosts").mkdir()
        with patch.object(bootstrap, "PATHS", SimpleNamespace(data_dir=Path(directory))):
            assert not bootstrap.is_bootstrapped(machine)
            bootstrap._save_bootstrapped(machine)
            assert bootstrap.is_bootstrapped(machine)
            expect_refusal(lambda: bootstrap.is_bootstrapped(
                replace(machine, address="192.0.2.23")
            ))
            assert "password" not in bootstrap.marker_path("nas").read_text()
            # Если назначение дисков осталось, но отметка SSH исчезла,
            # повторный парольный вход не должен запускаться.
            marker = bootstrap.marker_path("nas")
            (marker.parent / "nas-storage.json").write_text(
                json.dumps({"name": "nas", "devices": [], "volumes": {}}),
                encoding="utf-8",
            )
            marker.unlink()
            expect_refusal(lambda: bootstrap.is_bootstrapped(machine))


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
        (paths.data_dir / "physical-hosts").mkdir(parents=True)
        with (
            patch.object(operator, "PATHS", paths),
            patch.object(connection, "PATHS", paths),
        ):
            assert operator.operator_physical_bootstrap_state("nas") == 20
            marker = paths.data_dir / "physical-hosts/nas.json"
            marker.write_text(
                json.dumps({"name": "nas", "address": "192.0.2.22",
                            "fingerprint": "SHA256:" + "B" * 43}),
                encoding="utf-8",
            )
            assert operator.operator_physical_bootstrap_state("nas") == 0
            marker.write_text('{"name":"nas","address":"192.0.2.23"}')
            expect_refusal(lambda: operator.operator_physical_bootstrap_state("nas"))
            marker.unlink()
            (marker.parent / "nas-storage.json").write_text(
                json.dumps({"name": "nas", "devices": [], "volumes": {}}),
                encoding="utf-8",
            )
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



def test_connection_state() -> None:
    """Первичный адрес сохраняется без Git, повторная подмена невозможна."""

    machine = load_physical_host(ROOT, "nas")
    address, fingerprint = "192.0.2.22", "SHA256:" + "C" * 43
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "physical-hosts").mkdir()
        state_paths = SimpleNamespace(data_dir=root)
        operator_paths = SimpleNamespace(repo_root=ROOT, data_dir=root)
        with (
            patch.object(connection, "PATHS", state_paths),
            patch.object(operator, "PATHS", operator_paths),
            patch.object(connection, "_scan_verified_key", return_value=Path("/tmp/not-used")) as scan,
        ):
            assert operator.operator_physical_connection_state("nas") == 21
            connection.save_verified_connection(machine, address, fingerprint)
            assert scan.call_count == 1
            assert operator.operator_physical_connection_state("nas") == 0
            resolved = connection.apply_saved_connection(machine)
            assert resolved.address == address
            assert resolved.ssh_host_fingerprint == fingerprint
            assert "password" not in connection.connection_state_path("nas").read_text()
            expect_refusal(
                lambda: connection.save_verified_connection(machine, "192.0.2.23", fingerprint)
            )
            expect_refusal(
                lambda: connection.resolve_connection_values(
                    "nas", "192.0.2.23", None
                )
            )
            marker = root / "physical-hosts/nas.json"
            marker.write_text(
                json.dumps({"name": "nas", "address": address, "fingerprint": fingerprint}),
                encoding="utf-8",
            )
            connection.connection_state_path("nas").unlink()
            expect_refusal(lambda: connection.save_verified_connection(machine, address, fingerprint))
            expect_refusal(lambda: operator.operator_physical_connection_state("nas"))


def test_pve_connection_prompt() -> None:
    """Интерактивный SSH-адрес передаётся только закрытым вводом в 910."""

    calls = []
    address, fingerprint = "192.0.2.22", "SHA256:" + "C" * 43

    def forward(command, *, input_text=None):
        calls.append((command, input_text))
        return 21 if command == ["connection-state", "nas"] else 0

    with (
        patch.object(connection_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", side_effect=[address, fingerprint]),
    ):
        assert connection_prompt.initial_nas_connection(
            "nas", forward, host.OperatorError
        ) == 0
    assert calls == [
        (["connection-state", "nas"], None),
        (["connection-setup", "nas"], json.dumps({
            "address": address, "fingerprint": fingerprint,
        }) + "\n"),
    ]
    calls.clear()
    with patch.object(
        connection_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: False)
    ):
        try:
            connection_prompt.initial_nas_connection("nas", forward, host.OperatorError)
        except host.OperatorError as exc:
            assert "ssh -tt" in str(exc)
        else:
            raise AssertionError("Отсутствие интерактивного SSH-терминала принято")
    assert len(calls) == 1


def main() -> int:
    """Проверить контракт до испытаний на реальном NAS."""

    test_marker()
    test_operator_state()
    test_pve_password_transport()
    test_connection_state()
    test_pve_connection_prompt()
    print("[ОК] Первый SSH-доступ NAS проверен на испытательных данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
