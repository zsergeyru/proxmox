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
from infra_manager.physical_host_ssh import discover_first_host_key
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


def expect_refusal(callback, error_type=InfraManagerError) -> None:
    """Проверить ожидаемый безопасный отказ."""

    try:
        callback()
    except error_type:
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
            recovered = bootstrap.resolve_saved_host(replace(machine, ssh_host_fingerprint=None))
            assert recovered.ssh_host_fingerprint == machine.ssh_host_fingerprint
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
            "  ssh_host_fingerprint: null\n",
            encoding="utf-8",
        )
        paths = SimpleNamespace(repo_root=root, data_dir=root / "state")
        (paths.data_dir / "physical-hosts").mkdir(parents=True)
        with patch.object(operator, "PATHS", paths):
            assert operator.operator_physical_bootstrap_state("nas") == 20
            marker = paths.data_dir / "physical-hosts/nas.json"
            marker.write_text(
                json.dumps({"name": "nas", "address": "192.0.2.22",
                            "fingerprint": "SHA256:" + "B" * 43}),
                encoding="utf-8",
            )
            assert operator.operator_physical_bootstrap_state("nas") == 0
            target.joinpath("host.yaml").write_text(
                "connection:\n  address: 192.0.2.22\n"
                "  ssh_host_fingerprint: SHA256:" + "A" * 43 + "\n",
                encoding="utf-8",
            )
            expect_refusal(lambda: operator.operator_physical_bootstrap_state("nas"))
            target.joinpath("host.yaml").write_text(
                "connection:\n  address: 192.0.2.22\n"
                "  ssh_host_fingerprint: null\n",
                encoding="utf-8",
            )
            marker.write_text('{"name":"nas","address":"192.0.2.23"}')
            expect_refusal(lambda: operator.operator_physical_bootstrap_state("nas"))
            marker.unlink()
            (marker.parent / "nas-storage.json").write_text(
                json.dumps({"name": "nas", "devices": [], "volumes": {}}),
                encoding="utf-8",
            )
            expect_refusal(lambda: operator.operator_physical_bootstrap_state("nas"))


def test_another_physical_host_name() -> None:
    """Общий SSH-контур не должен содержать знания об имени nas."""

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        host_dir = root / "infrastructure/hosts/backup-node"
        host_dir.mkdir(parents=True)
        host_dir.joinpath("host.yaml").write_text(
            "connection:\n  address: 192.0.2.88\n"
            "  ssh_host_fingerprint: null\n", encoding="utf-8",
        )
        storage_dir = root / "state/physical-hosts"
        storage_dir.mkdir(parents=True)
        paths = SimpleNamespace(repo_root=root, data_dir=root / "state")
        with patch.object(operator, "PATHS", paths):
            assert operator.operator_physical_bootstrap_state("backup-node") == 20
            (storage_dir / "backup-node.json").write_text(
                json.dumps({
                    "name": "backup-node", "address": "192.0.2.88",
                    "fingerprint": "SHA256:" + "D" * 43,
                }), encoding="utf-8",
            )
            assert operator.operator_physical_bootstrap_state("backup-node") == 0


def test_automatic_initial_key() -> None:
    """Первый SSH-ключ принимается автоматически, но не два разных ключа."""

    machine = replace(
        load_physical_host(ROOT, "nas"),
        address="192.0.2.22", ssh_host_fingerprint=None,
    )
    responses = ["192.0.2.22 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAA\n"]

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[0] == "ssh-keyscan":
            assert argv[-1] == machine.address
            return SimpleNamespace(returncode=0, stdout=responses[0])
        if argv[0] == "ssh-keygen":
            return SimpleNamespace(
                returncode=0,
                stdout="256 SHA256:" + "C" * 43 + " NAS (ED25519)\n",
            )
        raise AssertionError("Посторонняя программа при принятии ключа")

    with patch("infra_manager.physical_host_ssh.run", side_effect=fake_run):
        trusted = discover_first_host_key(machine)
        assert trusted.ssh_host_fingerprint == "SHA256:" + "C" * 43
        expect_refusal(lambda: discover_first_host_key(trusted))
        responses[0] = (
            "192.0.2.22 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAA\n"
            "192.0.2.22 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBB\n"
        )
        expect_refusal(lambda: discover_first_host_key(machine))


def test_repeat_trust_requires_exact_scanned_key() -> None:
    """Записать новый ключ только после явного согласия и повторной проверки."""

    machine = replace(
        load_physical_host(ROOT, "nas"),
        address="192.0.2.22", ssh_host_fingerprint="SHA256:" + "B" * 43,
    )
    replacement = "SHA256:" + "C" * 43
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "physical-hosts").mkdir()
        with patch.object(bootstrap, "PATHS", SimpleNamespace(data_dir=Path(directory))):
            bootstrap._save_bootstrapped(machine)
            with patch.object(
                bootstrap, "discover_first_host_key",
                return_value=replace(machine, ssh_host_fingerprint=replacement),
            ):
                expect_refusal(lambda: bootstrap.confirm_redeploy_host_key(
                    machine, "SHA256:" + "D" * 43,
                ))
                assert bootstrap.resolve_saved_host(
                    replace(machine, ssh_host_fingerprint=None)
                ).ssh_host_fingerprint == machine.ssh_host_fingerprint
                changed = bootstrap.confirm_redeploy_host_key(
                    replace(machine, ssh_host_fingerprint=None), replacement,
                )
                assert changed
                assert bootstrap.resolve_saved_host(
                    replace(machine, ssh_host_fingerprint=None)
                ).ssh_host_fingerprint == replacement
                expect_refusal(lambda: bootstrap.confirm_redeploy_host_key(
                    machine, replacement,
                ))


def test_initial_root_without_sudo() -> None:
    """При root-входе sudo не нужен; диагностика отмечает неудачный этап."""

    machine = replace(
        load_physical_host(ROOT, "nas"),
        address="192.0.2.22", ssh_host_fingerprint="SHA256:" + "B" * 43,
    )
    commands = []
    writes = []
    exit_code = [0]

    class FakeChannel:
        def shutdown_write(self):
            pass

        def recv_exit_status(self):
            return exit_code[0]

    class FakeStdin:
        channel = FakeChannel()

        def write(self, value):
            writes.append(value)

        def flush(self):
            pass

    class FakeSSH:
        def load_host_keys(self, _path):
            pass

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **_kwargs):
            pass

        def exec_command(self, command, **_kwargs):
            commands.append(command)
            return FakeStdin(), SimpleNamespace(channel=FakeChannel()), None

        def close(self):
            pass

    fake_paramiko = SimpleNamespace(
        SSHClient=FakeSSH, RejectPolicy=lambda: None,
        SSHException=type("SSHException", (Exception,), {}),
    )
    with (
        patch.dict(sys.modules, {"paramiko": fake_paramiko}),
        patch.object(bootstrap, "_scan_verified_key", return_value=Path("/tmp/known_hosts")),
    ):
        bootstrap._install_client_ca(machine, "root", "test-password", "ssh-ed25519 AAAA")
        assert len(commands) == 1
        assert commands[0].startswith("sh -c ")
        assert "sudo -S" not in commands[0]
        assert "test-password" not in commands[0]
        assert "TrustedUserCAKeys" in commands[0]
        assert "PermitRootLogin prohibit-password" not in commands[0]
        assert "permitrootlogin (" not in commands[0]
        assert not writes

        commands.clear()
        bootstrap._install_client_ca(machine, "alice", "test-password", "ssh-ed25519 AAAA")
        assert commands[0].startswith("sudo -S -p '' sh -c ")
        assert writes == ["test-password\n"]

        exit_code[0] = 50
        try:
            bootstrap._install_client_ca(machine, "root", "test-password", "ssh-ed25519 AAAA")
        except InfraManagerError as exc:
            assert "sshd -t" in str(exc)
            assert "test-password" not in str(exc)
        else:
            raise AssertionError("Ошибка настройки sshd должна содержать этап")


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
    plan = {
        "name": "nas", "address": "192.0.2.22",
        "previous": "SHA256:" + "B" * 43,
        "current": "SHA256:" + "B" * 43,
        "changed": False,
    }

    def repeat_deploy(command: list[str], *, input_text: str | None = None,
                      capture_output: bool = False):
        calls.append((command, input_text))
        if command == ["ssh-trust-plan", "nas"]:
            assert capture_output
            return SimpleNamespace(
                returncode=0,
                stdout="PHYSICAL_SSH_TRUST_JSON=" + json.dumps(plan) + "\n",
                stderr="",
            )
        return 0

    with (
        patch.object(password_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: False)),
        patch("builtins.input", side_effect=AssertionError("Совпадающий ключ не требует ответа")),
        patch.object(password_prompt.getpass, "getpass") as password,
    ):
        assert password_prompt.initial_nas_access("nas", repeat_deploy, host.OperatorError) == 0
        password.assert_not_called()
    assert calls == [
        (["bootstrap-state", "nas"], None),
        (["ssh-trust-plan", "nas"], None),
    ]
    calls.clear()
    plan["changed"] = True
    plan["current"] = "SHA256:" + "C" * 43
    with (
        patch.object(password_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", return_value="да"),
    ):
        assert password_prompt.initial_nas_access("nas", repeat_deploy, host.OperatorError) == 0
    assert calls == [
        (["bootstrap-state", "nas"], None),
        (["ssh-trust-plan", "nas"], None),
        (["ssh-trust-confirm", "nas"], json.dumps({"fingerprint": plan["current"]}) + "\n"),
    ]
    calls.clear()
    with (
        patch.object(password_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", return_value="нет"),
    ):
        expect_refusal(
            lambda: password_prompt.initial_nas_access("nas", repeat_deploy, host.OperatorError),
            host.OperatorError,
        )
    assert all(command != ["ssh-trust-confirm", "nas"] for command, _ in calls)
    calls.clear()
    with patch.object(password_prompt.sys, "stdin", SimpleNamespace(isatty=lambda: False)):
        expect_refusal(
            lambda: password_prompt.initial_nas_access("nas", repeat_deploy, host.OperatorError),
            host.OperatorError,
        )
    assert all(command != ["ssh-trust-confirm", "nas"] for command, _ in calls)


def main() -> int:
    """Проверить контракт до испытаний на реальном NAS."""

    test_marker()
    test_operator_state()
    test_another_physical_host_name()
    test_automatic_initial_key()
    test_repeat_trust_requires_exact_scanned_key()
    test_initial_root_without_sudo()
    test_pve_password_transport()
    print("[ОК] Первый SSH-доступ NAS проверен на испытательных данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
