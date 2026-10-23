#!/usr/bin/env python3
"""Контракт автообновления SSH host certificate без связи с реальными гостями."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager import host_cert_renewal as renew
from infra_manager.common import InfraManagerError


def refuse(callback) -> None:
    try:
        callback()
    except InfraManagerError:
        return
    raise AssertionError("Небезопасное действие не было остановлено")


def test_persistent_pin() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(renew, "STATE", Path(directory)):
            key = "ssh-ed25519 " + "A" * 68
            assert renew._saved_pin(310, "gogs", "192.0.2.15") is None
            renew._save_pin(310, "gogs", "192.0.2.15", key)
            assert renew._saved_pin(310, "gogs", "192.0.2.15") == key
            assert (Path(directory) / "310.json").stat().st_mode & 0o777 == 0o600
            refuse(lambda: renew._saved_pin(310, "other", "192.0.2.15"))
            refuse(lambda: renew._saved_pin(310, "gogs", "192.0.2.16"))
            renew._save_pin(310, "gogs", "192.0.2.15", key)
            assert len(list(Path(directory).glob("310.json"))) == 1
            refuse(lambda: renew._pair("ssh-rsa " + "A" * 68))
            refuse(lambda: renew._saved_pin(-1, "gogs", "192.0.2.15"))


def test_remote_rollover_script() -> None:
    """Резервная копия и откат находятся в проверенном удалённом скрипте."""

    ast.parse(renew._INSTALL)
    assert 'current != pub' in renew._INSTALL
    assert 'os.replace(name, path)' in renew._INSTALL
    assert 'os.replace(old, path)' in renew._INSTALL
    assert 'sshd", "-t"' in renew._INSTALL
    assert '"systemctl", "reload", "ssh.service"' in renew._INSTALL
    assert 'hostcertificate /etc/ssh/ssh_host_ed25519_key-cert.pub' in renew._INSTALL
    assert 'mkfs' not in renew._INSTALL and 'zpool' not in renew._INSTALL


def test_valid_certificate_skips_renewal() -> None:
    key = "ssh-ed25519 " + "A" * 68
    issued = []
    sessions = []

    class FakeSession:
        def __init__(self, _node, address, pinned_host_key=None):
            sessions.append((address, pinned_host_key))
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def verify_connection(self):
            return True, ""

    def cat(_session, path):
        return key if path == renew.HOST_PUB else "SIGNED-CERT"
    with (
        patch.object(renew, "guest_operation_lock", return_value=nullcontext()),
        patch.object(renew, "GuestSshSession", FakeSession),
        patch.object(renew, "_saved_pin", return_value=key),
        patch.object(renew, "_cat", side_effect=cat),
        patch.object(renew, "_remaining", return_value=timedelta(days=22)),
        patch.object(renew, "sign_ssh_host_key", side_effect=lambda *a, **kw: issued.append((a,kw))),
    ):
        assert "22 дн." in renew._renew_one(310, "gogs", "192.0.2.15", "pve")
    assert not issued
    assert sessions == [("192.0.2.15", None)]


def test_expired_recovers_only_with_saved_key() -> None:
    key = "ssh-ed25519 " + "A" * 68
    wrong = "ssh-ed25519 " + "B" * 68
    replaced = [False]
    signed = []
    connections = []

    class FakeSession:
        def __init__(self, _node, address, pinned_host_key=None):
            self.pinned_host_key = pinned_host_key
            connections.append((address, pinned_host_key))
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def verify_connection(self):
            return replaced[0] or self.pinned_host_key == key, ""
        def ssh_command(self, cmd):
            return ["ssh", cmd]

    def cat(_session, path):
        return key if path == renew.HOST_PUB else "OLD-CERT"
    def fake_sign(node, public_key, **kwargs):
        signed.append((node, public_key, kwargs))
        return "NEW-CERT"
    def fake_run(args, **kwargs):
        assert "python3 -c" in args[-1]
        record = json.loads(kwargs["input_text"])
        assert record == {"public_key": key, "certificate": "NEW-CERT"}
        replaced[0] = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(renew, "guest_operation_lock", return_value=nullcontext()),
        patch.object(renew, "GuestSshSession", FakeSession),
        patch.object(renew, "_saved_pin", return_value=key),
        patch.object(renew, "_cat", side_effect=cat),
        patch.object(renew, "_remaining", return_value=timedelta(days=20)),
        patch.object(renew, "sign_ssh_host_key", side_effect=fake_sign),
        patch.object(renew, "run", side_effect=fake_run),
    ):
        assert renew._renew_one(310, "gogs", "192.0.2.15", "pve") == "восстановлен"
    assert len(signed) == 1
    assert signed[0][2] == {
        "vmid": 310, "hostname": "gogs", "address": "192.0.2.15",
    }
    assert connections[1] == ("192.0.2.15", key)
    assert connections[2] == ("192.0.2.15", key)
    assert connections[3] == ("192.0.2.15", None)
    replaced[0] = False
    connections.clear()
    with (
        patch.object(renew, "guest_operation_lock", return_value=nullcontext()),
        patch.object(renew, "GuestSshSession", FakeSession),
        patch.object(renew, "_saved_pin", return_value=None),
    ):
        refuse(lambda: renew._renew_one(310, "gogs", "192.0.2.15", "pve"))
    assert connections == [("192.0.2.15", None)]


def test_changed_key_rejected() -> None:
    key = "ssh-ed25519 " + "A" * 68
    wrong = "ssh-ed25519 " + "B" * 68
    calls = []

    class FakeSession:
        def __init__(self, _node, _address, pinned_host_key=None):
            self.pinned = pinned_host_key
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def verify_connection(self):
            return True, ""
    with (
        patch.object(renew, "guest_operation_lock", return_value=nullcontext()),
        patch.object(renew, "GuestSshSession", FakeSession),
        patch.object(renew, "_saved_pin", return_value=key),
        patch.object(renew, "_cat", return_value=wrong),
        patch.object(renew, "sign_ssh_host_key", side_effect=lambda *a, **k: calls.append(True)),
    ):
        refuse(lambda: renew._renew_one(310, "gogs", "192.0.2.15", "pve"))
    assert not calls


def main() -> None:
    test_persistent_pin()
    test_remote_rollover_script()
    test_valid_certificate_skips_renewal()
    test_expired_recovers_only_with_saved_key()
    test_changed_key_rejected()
    print("[ОК] Плановое продление SSH Host CA и безопасное восстановление проверены")


if __name__ == "__main__":
    main()
