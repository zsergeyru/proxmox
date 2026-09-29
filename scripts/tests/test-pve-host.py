#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

module = importlib.import_module("infra_manager.pve_host")


def test_feature_parser() -> None:
    parsed = module._features_from_config(
        "hostname: demo\nfeatures: nesting=1,keyctl=1\n"
    )
    assert parsed == {"nesting": "1", "keyctl": "1"}

    parsed = module._features_from_config(
        "features: fuse=1,nesting=1\n"
    )
    parsed["keyctl"] = "1"
    rendered = module._render_features(parsed)
    assert rendered == "fuse=1,keyctl=1,nesting=1"

    assert module._features_from_config("hostname: demo\n") == {}


def test_container_host_reconcile() -> None:
    calls: list[tuple[str, ...]] = []
    configs = iter(
        [
            "hostname: demo\nfeatures: nesting=1\n",
            "hostname: demo\nfeatures: keyctl=1,nesting=1\n",
        ]
    )

    def fake_ssh(node: str, *command: str, capture: bool = False):
        del node, capture
        calls.append(tuple(command))
        if command[:2] == ("pct", "config"):
            return SimpleNamespace(returncode=0, stdout=next(configs))
        if command[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: running\n")
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(module, "_ssh", fake_ssh):
        module.ensure_container_host("pve", 420)

    expected = [
        ("pct", "config", "420"),
        ("pct", "status", "420"),
        ("pct", "stop", "420"),
        ("pct", "set", "420", "--features", "keyctl=1,nesting=1"),
        ("pct", "start", "420"),
        ("pct", "config", "420"),
    ]
    assert calls == expected, calls


def test_container_host_noop() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_ssh(node: str, *command: str, capture: bool = False):
        del node, capture
        calls.append(tuple(command))
        return SimpleNamespace(
            returncode=0,
            stdout="features: keyctl=1,nesting=1\n",
        )

    with patch.object(module, "_ssh", fake_ssh):
        module.ensure_container_host("pve", 420)

    assert calls == [("pct", "config", "420")], calls


def test_infra_self_access() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_ssh(node: str, *command: str, capture: bool = False):
        del node, capture
        calls.append(tuple(command))
        if command[:2] == ("pct", "config"):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "hostname: infra-manager\n"
                    "description: test "
                    "[owner=proxmox-project;role=infra-manager]\n"
                ),
            )
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(module, "_ssh", fake_ssh):
        module.ensure_infra_self_access(
            "pve",
            910,
            hostname="infra-manager",
            public_key="ssh-ed25519 AAAATEST old-comment",
        )

    assert calls[0] == ("pct", "config", "910")
    exec_call = calls[1]
    assert exec_call[:5] == (
        "pct",
        "exec",
        "910",
        "--",
        "python3",
    )
    assert exec_call[-1] == "ssh-ed25519 AAAATEST"

    with patch.object(
        module,
        "_ssh",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="hostname: чужой\n",
        ),
    ):
        try:
            module.ensure_infra_self_access(
                "pve",
                910,
                hostname="infra-manager",
                public_key="ssh-ed25519 AAAATEST",
            )
        except module.InfraManagerError:
            pass
        else:
            raise AssertionError(
                "Самодоступ нельзя выдавать непроверенному VMID 910"
            )


def test_openbao_host_support() -> None:
    installs: list[tuple[str, str, str]] = []
    ssh_calls: list[tuple[str, ...]] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        command = root / "scripts/infra-manager/host/openbao-unseal.py"
        command.parent.mkdir(parents=True)
        command.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        def record_install(
            node: str,
            source: Path,
            target: Path,
            mode: str,
        ) -> None:
            assert node == "pve"
            installs.append((str(source), str(target), mode))

        def record_ssh(
            node: str,
            *command_args: str,
            capture: bool = False,
        ):
            del capture
            assert node == "pve"
            ssh_calls.append(tuple(command_args))
            return SimpleNamespace(returncode=0, stdout="")

        with (
            patch.object(module, "_install_remote_file", side_effect=record_install),
            patch.object(module, "_ssh", side_effect=record_ssh),
        ):
            module.install_openbao_host_support("pve", root)

    assert installs == [
        (
            str(command),
            "/usr/local/sbin/infra-manager-openbao-unseal",
            "0755",
        ),
    ]
    assert len(ssh_calls) == 1
    assert ssh_calls[0][:2] == ("sh", "-c")
    cleanup = ssh_calls[0][2]
    assert "infra-manager-openbao-unseal.timer" in cleanup
    assert "infra-manager-openbao-unseal.service" in cleanup

    with patch.object(module, "_ssh") as mocked:
        module.initialize_openbao_on_host("pve")
        mocked.assert_called_once_with(
            "pve",
            "/usr/local/sbin/infra-manager-openbao-unseal",
            "--initialize",
        )

    with patch.object(module, "_ssh") as mocked:
        module.trigger_openbao_unseal("pve")
        mocked.assert_called_once_with(
            "pve",
            "/usr/local/sbin/infra-manager-openbao-unseal",
        )


def test_sign_ssh_client_key() -> None:
    calls: list[tuple[tuple[str, ...], str]] = []

    def fake_input(
        node: str,
        *command: str,
        input_text: str,
    ):
        assert node == "pve"
        calls.append((tuple(command), input_text))
        return SimpleNamespace(
            returncode=0,
            stdout="ssh-ed25519-cert-v01@openssh.com AAAATEST\n",
        )

    with patch.object(module, "_ssh_with_input", side_effect=fake_input):
        certificate = module.sign_ssh_client_key(
            "pve",
            "ssh-ed25519 AAAAPUBLIC temporary",
        )

    assert certificate == "ssh-ed25519-cert-v01@openssh.com AAAATEST"
    assert calls == [
        (
            (
                "/usr/local/sbin/infra-manager-openbao-unseal",
                "--sign-client-key",
            ),
            "ssh-ed25519 AAAAPUBLIC temporary\n",
        )
    ]


def main() -> None:
    test_feature_parser()
    test_container_host_reconcile()
    test_container_host_noop()
    test_infra_self_access()
    test_openbao_host_support()
    test_sign_ssh_client_key()
    print("[ОК] Проверки pve_host.py пройдены")


if __name__ == "__main__":
    main()
