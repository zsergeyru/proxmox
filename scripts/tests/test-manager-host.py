#!/usr/bin/env python3
"""Проверки единой операторской команды infra-manager на PVE."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "scripts" / "infra-manager" / "host" / "manager.py"

spec = importlib.util.spec_from_file_location("infra_manager_host_manager", TARGET)
if spec is None or spec.loader is None:
    raise SystemExit(f"Не удалось загрузить {TARGET}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_verify_guest_owned() -> None:
    good = (
        "hostname: infra-manager\n"
        "unprivileged: 1\n"
        "description: test [owner=proxmox-project;role=infra-manager]\n"
    )
    with patch.object(module, "guest_config", return_value=good):
        module.verify_guest_owned(910, "infra-manager")

    with patch.object(module, "guest_config", return_value=None):
        try:
            module.verify_guest_owned(910, "infra-manager")
        except module.OperatorError as exc:
            assert "infra-manager recover" in str(exc)
        else:
            raise AssertionError("Отсутствующий 910 должен направлять в recover")

    foreign = (
        "hostname: foreign\n"
        "unprivileged: 1\n"
        "description: чужой объект\n"
    )
    with patch.object(module, "guest_config", return_value=foreign):
        try:
            module.verify_guest_owned(910, "infra-manager")
        except module.OperatorError:
            pass
        else:
            raise AssertionError("Чужой VMID нельзя автоматически изменять")


def test_status() -> None:
    recovery_calls: list[list[str]] = []
    guest_calls: list[tuple[object, ...]] = []

    def fake_run(argv: list[str], **kwargs: object):
        del kwargs
        recovery_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_exec(vmid: int, *argv: str, **kwargs: object):
        del kwargs
        guest_calls.append((vmid, *argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(module, "load_identity", return_value=(920, "infra-manager")),
        patch.object(module, "run", side_effect=fake_run),
        patch.object(module, "verify_guest_owned"),
        patch.object(module, "guest_running", return_value=True),
        patch.object(module, "exec_guest", side_effect=fake_exec),
    ):
        assert module.status() == 0

    assert recovery_calls == [
        ["/usr/local/sbin/infra-manager-recovery", "--check"]
    ]
    assert guest_calls == [
        (920, "infra-manager-status", "--full")
    ]


def test_repair() -> None:
    recovery_modes: list[str] = []
    host_calls: list[list[str]] = []
    guest_calls: list[tuple[object, ...]] = []

    def fake_run(argv: list[str], **kwargs: object):
        del kwargs
        host_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_exec(vmid: int, *argv: str, **kwargs: object):
        del kwargs
        guest_calls.append((vmid, *argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(module, "load_identity", return_value=(910, "infra-manager")),
        patch.object(
            module,
            "recovery_check",
            side_effect=lambda mode: recovery_modes.append(mode),
        ),
        patch.object(module, "verify_guest_owned"),
        patch.object(module, "_wait_running"),
        patch.object(module, "exec_guest", side_effect=fake_exec),
        patch.object(module, "require_executable"),
        patch.object(module, "run", side_effect=fake_run),
        patch.object(module, "_project_branch", return_value="feature/test"),
        patch.object(module.socket, "gethostname", return_value="pve.example"),
        patch.object(module, "status", return_value=0),
    ):
        assert module.repair() == 0

    assert recovery_modes == ["--preflight"]
    assert [
        "/usr/local/sbin/infra-manager-openbao-unseal",
        "--initialize",
        "--log-level",
        "normal",
    ] in host_calls
    assert (
        910,
        "env",
        "INFRA_PROJECT_BRANCH=feature/test",
        "INFRA_PVE_NODE=pve",
        "/usr/local/sbin/infra-manager-activate-runtime",
    ) in guest_calls


def test_recover() -> None:
    recovery_modes: list[str] = []
    host_calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object):
        del kwargs
        host_calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_download(target: Path) -> None:
        target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    with (
        patch.object(
            module,
            "recovery_check",
            side_effect=lambda mode: recovery_modes.append(mode),
        ),
        patch.object(module, "_download_bootstrap", side_effect=fake_download),
        patch.object(module, "run", side_effect=fake_run),
        patch.object(module, "status", return_value=0),
    ):
        assert module.recover() == 0

    assert recovery_modes == ["--preflight", "--restore-git-access"]
    assert len(host_calls) == 1
    assert host_calls[0][0] == "bash"
    assert host_calls[0][-1] == "--recover"


def main() -> None:
    test_verify_guest_owned()
    test_status()
    test_repair()
    test_recover()
    print("[ОК] Единая операторская команда PVE проверена")


if __name__ == "__main__":
    main()
