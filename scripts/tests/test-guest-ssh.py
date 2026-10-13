#!/usr/bin/env python3
"""Проверить единый SSH-сеанс гостей без сети и без действующих секретов."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.guest_ssh import GuestSshSession


def check_managed_session() -> None:
    """Старый ключ гостя не должен влиять на сертификатную проверку."""
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        ca = directory / "ssh-host-ca.pub"
        ca.write_text("ssh-ed25519 AAAAHOSTCA root@ca\n", encoding="utf-8")
        stale = directory / "old_known_hosts"
        stale.write_text("192.168.9.10 ssh-ed25519 AAAAOLD\n", encoding="utf-8")
        seen: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            """Заменить системный вызов без внешнего SSH-соединения."""
            seen.append(argv)
            if argv[0] == "ssh-keygen" and "-t" in argv:
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("private", encoding="utf-8")
                Path(str(key) + ".pub").write_text(
                    "ssh-ed25519 AAAACLIENT temporary\n", encoding="utf-8"
                )
            return SimpleNamespace(returncode=0, stdout="valid", stderr="")

        with GuestSshSession(
            "pve", "192.168.9.10", host_ca=ca,
            sign_client=lambda node, key: "ssh-ed25519-cert-v01@openssh.com AAAACERT",
            execute=fake_run,
        ) as session:
            key = session.private_key
            hosts = session.known_hosts
            assert key is not None and hosts is not None
            assert session.certificate is not None
            assert hosts.read_text() == (
                "@cert-authority 192.168.9.10 ssh-ed25519 AAAAHOSTCA root@ca\n"
            )
            assert stale.read_text() == "192.168.9.10 ssh-ed25519 AAAAOLD\n"
            args = session.ssh_command("true")
            joined = " ".join(args)
            assert "HostKeyAlgorithms=ssh-ed25519-cert-v01@openssh.com" in joined
            assert "GlobalKnownHostsFile=/dev/null" in joined
            assert "CertificateFile=" in joined
            assert str(stale) not in joined
            assert session.ansible_ssh_args().find(str(hosts)) >= 0
            assert session.verify_connection() == (True, "")
        assert not key.exists() and not hosts.exists()
        assert not any(command[0] == "ssh-keyscan" for command in seen)


def check_bootstrap_session() -> None:
    """Начальный ключ допустим только с заранее подтверждённым сервером."""
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        key = directory / "one_time_key"
        key.write_text("private", encoding="utf-8")
        hosts = directory / "known_hosts"
        hosts.write_text("192.0.2.1 ssh-ed25519 AAAAPIN\n", encoding="utf-8")
        with GuestSshSession(
            "pve", "192.0.2.1", bootstrap_key=key,
            bootstrap_known_hosts=hosts,
        ) as session:
            ssh = " ".join(session.ssh_command("true"))
            assert "CertificateFile=" not in ssh
            assert "HostKeyAlgorithms=ssh-ed25519-cert" not in ssh
        assert key.is_file() and hosts.is_file()
        try:
            with GuestSshSession(
                "pve", "192.0.2.1", bootstrap_key=key,
                bootstrap_known_hosts=directory / "missing",
            ):
                pass
        except InfraManagerError:
            pass
        else:
            raise AssertionError("Начальный SSH без known_hosts не отклонён")


if __name__ == "__main__":
    check_managed_session()
    check_bootstrap_session()
    print("Guest SSH session checks passed")
