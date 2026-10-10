#!/usr/bin/env python3
"""Ограниченный клиент infra-manager для контейнера Hermes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import ssl
import subprocess
import sys
import tempfile
from urllib import error, request

CONFIG = Path("/opt/data/skills/infra-manager/identity.json")
CA = Path("/opt/data/skills/infra-manager/ca.crt")
KNOWN_HOSTS = Path("/opt/data/skills/infra-manager/known_hosts")
COMMANDS = {"guests", "status", "test", "deploy", "sync", "repair"}


def _post(url: str, payload: dict, context: ssl.SSLContext, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    req = request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with request.urlopen(req, timeout=15, context=context) as response:
        return json.load(response)


def run(operation: str, vmid: str | None) -> int:
    if operation not in COMMANDS:
        raise ValueError("Неизвестная операция")
    if operation == "guests" and vmid is not None:
        raise ValueError("guests не принимает VMID")
    if operation not in {"guests", "status"} and vmid is None:
        raise ValueError("Для операции необходим VMID")
    if vmid is not None and (not vmid.isascii() or not vmid.isdecimal()):
        raise ValueError("VMID должен быть числом")

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    url = config["openbao_url"].rstrip("/")
    context = ssl.create_default_context(cafile=str(CA))
    with tempfile.TemporaryDirectory(prefix="pve-agent-") as name:
        path = Path(name)
        private_key = path / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
            check=True,
        )
        login = _post(url + "/v1/auth/machine/login", {
            "role_id": config["role_id"], "secret_id": config["secret_id"]
        }, context)
        token = login["auth"]["client_token"]
        try:
            result = _post(url + "/v1/ssh-client-signer/sign/pve-agent", {
                "cert_type": "user",
                "public_key": (path / "id_ed25519.pub").read_text(encoding="utf-8").strip(),
                "valid_principals": "infra-agent",
                "ttl": "2m",
            }, context, token)
            certificate = path / "id_ed25519-cert.pub"
            certificate.write_text(result["data"]["signed_key"] + "\n", encoding="utf-8")
        finally:
            _post(url + "/v1/auth/token/revoke-self", {}, context, token)

        cmd = ["infra-manager", operation]
        if vmid is not None:
            cmd.append(vmid)
        return subprocess.run([
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ClearAllForwardings=yes",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-i", str(private_key),
            "-o", f"CertificateFile={certificate}",
            "infra-agent@" + config["pve_host"],
            shlex.join(cmd),
        ], check=False).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ограниченные команды infra-manager")
    parser.add_argument("operation", choices=sorted(COMMANDS))
    parser.add_argument("vmid", nargs="?")
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.operation, args.vmid))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, error.URLError) as exc:
        print("ОШИБКА: вызов infra-manager не выполнен", file=sys.stderr)
        raise SystemExit(1) from exc
