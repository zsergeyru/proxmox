#!/usr/bin/env python3
"""Проверки клиента Hermes без вызова реального OpenBao или SSH."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "automation/ansible/roles/ai_control/files/pve-infra-manager.py"


def load_client():
    spec = importlib.util.spec_from_file_location("pve_agent_client", CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client = load_client()


def check_commands() -> None:
    for op, vmid in (
        ("guests", "340"),
        ("deploy", None),
        ("status", "-1"),
        ("status", "../etc/passwd"),
        ("deploy", "340;id"),
        ("recover", None),
    ):
        try:
            client.run(op, vmid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Операция {op} {vmid} не отклонена")


def check_empty_openbao_response() -> None:
    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b""

    with patch.object(client.request, "urlopen", return_value=EmptyResponse()):
        assert client._post(
            "https://192.0.2.10:8202/v1/auth/token/revoke-self", {}, object()
        ) == {}


def check_verified_transient_certificate() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        identity = root / "identity.json"
        ca = root / "ca.crt"
        known_hosts = root / "known_hosts"
        identity.write_text(json.dumps({
            "openbao_url": "https://192.0.2.10:8202",
            "role_id": "test-role",
            "secret_id": "test-secret",
            "pve_host": "192.0.2.9",
        }), encoding="utf-8")
        ca.write_text("dummy trust\n", encoding="utf-8")
        known_hosts.write_text("192.0.2.9 ssh-ed25519 dummy\n", encoding="utf-8")

        http_calls = []
        commands = []
        def fake_post(url, payload, context, token=None):
            http_calls.append((url, payload, token))
            if url.endswith("auth/machine/login"):
                return {"auth": {"client_token": "short-token"}}
            if url.endswith("sign/pve-agent"):
                return {"data": {"signed_key": "ssh-ed25519-cert-v01@openssh.com TEST"}}
            return {}
        def fake_run(argv, **kwargs):
            commands.append(argv)
            if argv[0] == "ssh-keygen":
                key = Path(argv[-1])
                key.write_text("test private key", encoding="utf-8")
                key.with_suffix(key.suffix + ".pub").write_text(
                    "ssh-ed25519 TEST generated", encoding="utf-8"
                )
            return SimpleNamespace(returncode=0)

        with (
            patch.object(client, "CONFIG", identity),
            patch.object(client, "CA", ca),
            patch.object(client, "KNOWN_HOSTS", known_hosts),
            patch.object(client, "_post", side_effect=fake_post),
            patch.object(client.ssl, "create_default_context", return_value=object()),
            patch.object(client.subprocess, "run", side_effect=fake_run),
        ):
            assert client.run("status", "340") == 0

        assert len(commands) == 2
        cmd = commands[1]
        assert cmd[0] == "ssh"
        assert "-T" in cmd
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=yes" in cmd
        assert f"UserKnownHostsFile={known_hosts}" in cmd
        assert cmd[-2:] == ["infra-agent@192.0.2.9", "infra-manager status 340"]
        assert len(http_calls) == 3
        assert http_calls[0][0].endswith("/v1/auth/machine/login")
        assert http_calls[1][0].endswith("/v1/ssh-client-signer/sign/pve-agent")
        assert http_calls[1][1]["valid_principals"] == "infra-agent"
        assert http_calls[2][0].endswith("/v1/auth/token/revoke-self")
        assert "test-secret" not in " ".join(" ".join(parts) for parts in commands)


def main() -> None:
    check_commands()
    check_empty_openbao_response()
    check_verified_transient_certificate()
    print("Hermes restricted operator client checks passed.")


if __name__ == "__main__":
    main()
