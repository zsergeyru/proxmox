#!/usr/bin/env python3
"""Контрактные проверки SSH-клиента OpenBao OTP."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CLIENT = (
    ROOT
    / "automation/ansible/roles/linux_base/files/openbao-ssh.py"
)


def fail(message: str) -> None:
    raise SystemExit(message)


def load_client():
    spec = importlib.util.spec_from_file_location("openbao_ssh_test", CLIENT)
    if spec is None or spec.loader is None:
        fail("Не удалось загрузить SSH-клиент OpenBao OTP")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strict_ssh_arguments() -> None:
    client = load_client()
    args = client.ssh_arguments("192.168.3.11", 17, ["true"])
    text = " ".join(args)
    required = (
        "sshpass -d 17 ssh",
        "PreferredAuthentications=keyboard-interactive",
        "PubkeyAuthentication=no",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        f"UserKnownHostsFile={client.KNOWN_HOSTS}",
        "root@192.168.3.11 true",
    )
    for item in required:
        if item not in text:
            fail(f"SSH OTP не содержит обязательный параметр: {item}")
    if "OTP-SECRET" in text:
        fail("OTP не должен находиться в argv")


def test_target_must_be_exactly_trusted() -> None:
    client = load_client()
    with tempfile.TemporaryDirectory() as temporary:
        known = Path(temporary) / "known_hosts"
        known.write_text(
            "@cert-authority 192.168.3.11 ssh-ed25519 AAAATEST\n",
            encoding="utf-8",
        )
        if not client.target_is_trusted("192.168.3.11", known):
            fail("Разрешённая SSH-цель не распознана")
        if client.target_is_trusted("192.168.3.12", known):
            fail("Неизвестная SSH-цель неожиданно разрешена")


def test_rejects_non_ipv4_target_before_otp() -> None:
    client = load_client()
    with patch.object(client, "request_otp") as request_otp:
        for target in ("example.org", "2001:db8::1"):
            try:
                client.run_ssh(target, [])
            except client.OtpSshError:
                pass
            else:
                fail(f"Принята недопустимая SSH-цель: {target}")
    request_otp.assert_not_called()


def main() -> None:
    test_strict_ssh_arguments()
    test_target_must_be_exactly_trusted()
    test_rejects_non_ipv4_target_before_otp()
    print("OpenBao OTP SSH client tests passed.")


if __name__ == "__main__":
    main()
