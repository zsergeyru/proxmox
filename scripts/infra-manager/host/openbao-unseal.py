#!/usr/bin/env python3
"""Инициализация и разблокировка OpenBao из доверенного PVE-хоста."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

VMID = 910
OPENBAO_URL = "http://127.0.0.1:8200"
KEY_DIR = Path("/mnt/bindmounts/infra-manager/pve-only/openbao")
KEY_PATH = KEY_DIR / "unseal.key"
SSH_ACCESS_PATH = KEY_DIR / "ssh-access.json"
KV_ACCESS_PATH = KEY_DIR / "kv-access.json"
TLS_PVE_ONLY_DIR = Path("/mnt/bindmounts/infra-manager/pve-only/openbao-tls")
TLS_ACCESS_DIR = Path("/mnt/bindmounts/infra-manager/access/openbao-tls")
TLS_CA_KEY = TLS_PVE_ONLY_DIR / "ca.key"
TLS_CA_CERT = TLS_PVE_ONLY_DIR / "ca.crt"
TLS_ACCESS_CA_CERT = TLS_ACCESS_DIR / "ca.crt"
TLS_SERVER_KEY = TLS_ACCESS_DIR / "server.key"
TLS_SERVER_CERT = TLS_ACCESS_DIR / "server.crt"
TLS_ADDRESS = "192.168.9.10"
TLS_DNS_NAME = "infra-manager"
LOCK_PATH = Path("/run/lock/infra-manager-openbao-unseal.lock")
CT_RUNTIME_DIR = Path("/run/infra-manager")
CT_KEY_PATH = CT_RUNTIME_DIR / "openbao-unseal.key"
CT_COMPAT_CONFIG_PATH = CT_RUNTIME_DIR / "openbao-generate-root.hcl"
CT_OPENBAO_CONFIG_PATH = Path("/opt/infra-manager/compose/openbao/openbao.hcl")
CT_OPENBAO_DATA_PATH = Path("/mnt/persistent-state/openbao")
CT_CLIENT_CA_PATH = Path("/etc/infra-manager/ca/ssh-client-ca.pub")
CT_HOST_CA_PATH = Path("/etc/infra-manager/ca/ssh-host-ca.pub")
TEMP_OPENBAO_CONTAINER = "infra-manager-openbao-generate-root"
KV_MOUNT = "infra-secrets"
RUNTIME_SECRET_DIR = Path("/run/infra-manager/secrets")
LOG_LEVEL = "normal"

STATUS_CODE = r"""
import urllib.request
with urllib.request.urlopen(
    "http://127.0.0.1:8200/v1/sys/seal-status",
    timeout=5,
) as response:
    print(response.read().decode("utf-8"))
"""

INIT_CODE = r"""
import json
import urllib.request
payload = json.dumps(
    {"secret_shares": 1, "secret_threshold": 1},
    separators=(",", ":"),
).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/sys/init",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
"""

UNSEAL_CODE = r"""
import json
import pathlib
import urllib.request
key = pathlib.Path("/run/infra-manager/openbao-unseal.key").read_text(
    encoding="utf-8"
).strip()
payload = json.dumps({"key": key}, separators=(",", ":")).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/sys/unseal",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
"""

PREPARE_GENERATE_ROOT_CONFIG_CODE = r"""
from pathlib import Path

source = Path("/opt/infra-manager/compose/openbao/openbao.hcl")
target = Path("/run/infra-manager/openbao-generate-root.hcl")

text = source.read_text(encoding="utf-8")
if 'address         = "127.0.0.1:8200"' not in text:
    raise SystemExit("generate-root compatibility mode requires loopback listener")
if "disable_unauthed_generate_root_endpoints" in text:
    raise SystemExit("permanent OpenBao config already changes generate-root protection")

needle = "  tls_disable     = true\n"
if text.count(needle) != 1:
    raise SystemExit("OpenBao listener is not uniquely defined")

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(
    text.replace(
        needle,
        "  disable_unauthed_generate_root_endpoints = false\n" + needle,
        1,
    ),
    encoding="utf-8",
)
target.chmod(0o644)
"""


REVOKE_ROOT_CODE = r"""
import sys
import urllib.request
token = sys.stdin.read().strip()
if not token:
    raise SystemExit("empty token")
request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/auth/token/revoke-self",
    headers={"X-Vault-Token": token},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    response.read()
"""


GENERATE_ROOT_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
unseal_key = sys.stdin.read().strip()
if not unseal_key:
    raise SystemExit("empty unseal key")


def request(method, path, payload=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


attempt = request("GET", "/v1/sys/generate-root/attempt")
if attempt.get("started") is True:
    raise SystemExit("root token generation is already in progress")

started = request("POST", "/v1/sys/generate-root/attempt", {})
otp = started.get("otp")
nonce = started.get("nonce")
if not isinstance(otp, str) or not otp:
    raise SystemExit("OpenBao did not return OTP")
if not isinstance(nonce, str) or not nonce:
    raise SystemExit("OpenBao did not return nonce")

completed = request(
    "POST",
    "/v1/sys/generate-root/update",
    {"key": unseal_key, "nonce": nonce},
)
if completed.get("complete") is not True:
    raise SystemExit("root token generation did not reach threshold")

encoded = completed.get("encoded_token")
if not isinstance(encoded, str) or not encoded:
    raise SystemExit("OpenBao did not return encoded root token")

decoded = request(
    "POST",
    "/v1/sys/decode-token",
    {"encoded_token": encoded, "otp": otp},
)
data = decoded.get("data")
token = data.get("token") if isinstance(data, dict) else None
if not isinstance(token, str) or not token or any(char.isspace() for char in token):
    raise SystemExit("OpenBao did not decode a valid root token")

print(token)
"""


SSH_CA_STATUS_CODE = r"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"


def public_key(mount):
    try:
        with urllib.request.urlopen(
            f"{BASE}/v1/{mount}/public_key",
            timeout=10,
        ) as response:
            return response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in {403, 404}:
            return ""
        if exc.code == 400 and "no default issuer currently configured" in detail:
            return ""
        raise


client = public_key("ssh-client-signer")
host = public_key("ssh-host-signer")
valid = lambda value: value.startswith(("ssh-", "ecdsa-", "sk-"))
print(
    json.dumps(
        {
            "client": valid(client),
            "host": valid(host),
            "distinct": bool(client and host and client != host),
        },
        separators=(",", ":"),
    )
)
"""

READ_CLIENT_CA_CODE = r"""
import sys
import urllib.request

token = sys.stdin.read().strip()
if not token:
    raise SystemExit("empty root token")

request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/ssh-client-signer/public_key",
    headers={"X-Vault-Token": token},
    method="GET",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8").strip())
"""


READ_HOST_CA_CODE = r"""
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/ssh-host-signer/public_key",
    method="GET",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8").strip())
"""


CONFIGURE_SSH_CA_CODE = r"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
token = sys.stdin.read().strip()
if not token:
    raise SystemExit("empty root token")


def request(
    method,
    path,
    payload=None,
    *,
    authenticated=True,
    allow_missing_ca=False,
):
    data = None
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["X-Vault-Token"] = token
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if allow_missing_ca and exc.code == 404:
            return exc.code, b""
        if (
            allow_missing_ca
            and exc.code == 400
            and "no default issuer currently configured" in detail
        ):
            return exc.code, b""
        raise RuntimeError(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc


def public_key(mount):
    status, raw = request(
        "GET",
        f"/v1/{mount}/public_key",
        authenticated=False,
        allow_missing_ca=True,
    )
    if status in {400, 404}:
        return ""
    return raw.decode("utf-8").strip()


_, raw_mounts = request("GET", "/v1/sys/mounts")
payload = json.loads(raw_mounts.decode("utf-8"))
mounts = payload.get("data", payload)
if not isinstance(mounts, dict):
    raise SystemExit("OpenBao returned invalid mounts list")

definitions = (
    (
        "ssh-client-signer",
        "Подпись временных SSH-сертификатов для входа в управляемые гости",
    ),
    (
        "ssh-host-signer",
        "Подпись SSH-сертификатов управляемых серверов",
    ),
)

for mount, description in definitions:
    existing = mounts.get(f"{mount}/")
    if existing is None:
        request(
            "POST",
            f"/v1/sys/mounts/{mount}",
            {"type": "ssh", "description": description},
        )
    elif not isinstance(existing, dict) or existing.get("type") != "ssh":
        raise SystemExit(f"{mount} exists with unexpected type")

    if not public_key(mount):
        request(
            "POST",
            f"/v1/{mount}/config/ca",
            {"generate_signing_key": True},
        )

client = public_key("ssh-client-signer")
host = public_key("ssh-host-signer")
valid = lambda value: value.startswith(("ssh-", "ecdsa-", "sk-"))
if not valid(client) or not valid(host):
    raise SystemExit("OpenBao SSH CA public keys are missing")
if client == host:
    raise SystemExit("OpenBao SSH CA public keys must be different")

print(
    json.dumps(
        {
            "client_public_key": client,
            "host_public_key": host,
        },
        separators=(",", ":"),
    )
)
"""


CONFIGURE_SSH_ACCESS_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid configuration payload")
token = payload.get("root_token")
existing_credentials = payload.get("existing_credentials", {})
if not isinstance(token, str) or not token:
    raise SystemExit("empty root token")
if not isinstance(existing_credentials, dict):
    raise SystemExit("invalid existing credentials")


def request(method, path, payload=None):
    data = None
    headers = {
        "Content-Type": "application/json",
        "X-Vault-Token": token,
    }
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


config_policy = json.dumps(
    {
        "path": {
            "ssh-client-signer/roles": {
                "capabilities": ["list"],
            },
            "ssh-client-signer/roles/*": {
                "capabilities": ["create", "read", "update", "delete"],
            },
            "ssh-host-signer/roles": {
                "capabilities": ["list"],
            },
            "ssh-host-signer/roles/*": {
                "capabilities": ["create", "read", "update", "delete"],
            },
            "auth/token/lookup-self": {
                "capabilities": ["read"],
            },
            "auth/token/revoke-self": {
                "capabilities": ["update"],
            },
            "sys/capabilities-self": {
                "capabilities": ["update"],
            },
        }
    },
    separators=(",", ":"),
)

signer_policy = json.dumps(
    {
        "path": {
            "ssh-client-signer/sign/infra-manager": {
                "capabilities": ["create", "update"],
            },
            "ssh-client-signer/sign/machine-*": {
                "capabilities": ["create", "update"],
            },
            "ssh-host-signer/sign/managed-host": {
                "capabilities": ["create", "update"],
            },
            "auth/token/lookup-self": {
                "capabilities": ["read"],
            },
            "auth/token/revoke-self": {
                "capabilities": ["update"],
            },
            "sys/capabilities-self": {
                "capabilities": ["update"],
            },
        }
    },
    separators=(",", ":"),
)

otp_config_policy = json.dumps(
    {
        "path": {
            "sys/mounts": {"capabilities": ["read"]},
            "sys/auth": {"capabilities": ["read"]},
            "ssh-otp/roles": {"capabilities": ["list"]},
            "ssh-otp/roles/guest-*": {
                "capabilities": ["create", "read", "update", "delete"],
            },
            "auth/machine/role": {"capabilities": ["list"]},
            "auth/machine/role/guest-*": {
                "capabilities": ["create", "read", "update", "delete"],
                "required_parameters": [
                    "bind_secret_id",
                    "secret_id_bound_cidrs",
                    "token_bound_cidrs",
                    "token_policies",
                    "token_ttl",
                    "token_max_ttl",
                    "token_no_default_policy",
                ],
                "allowed_parameters": {
                    "bind_secret_id": [True],
                    "secret_id_bound_cidrs": [],
                    "secret_id_num_uses": [0],
                    "secret_id_ttl": ["0s"],
                    "token_bound_cidrs": [],
                    "token_num_uses": [0],
                    "token_policies": ["machine-ssh-otp"],
                    "token_ttl": ["5m"],
                    "token_max_ttl": ["10m"],
                    "token_no_default_policy": [True],
                },
            },
            "auth/machine/role/guest-*/role-id": {
                "capabilities": ["read"],
            },
            "auth/machine/role/guest-*/secret-id": {
                "capabilities": ["create", "update"],
            },
            "auth/token/lookup-self": {"capabilities": ["read"]},
            "auth/token/revoke-self": {"capabilities": ["update"]},
            "sys/capabilities-self": {"capabilities": ["update"]},
        }
    },
    separators=(",", ":"),
)

request(
    "POST",
    "/v1/sys/policies/acl/infra-manager-ssh-ca-config",
    {"policy": config_policy},
)
request(
    "POST",
    "/v1/sys/policies/acl/infra-manager-ssh-signer",
    {"policy": signer_policy},
)
request(
    "POST",
    "/v1/sys/policies/acl/infra-manager-ssh-otp-config",
    {"policy": otp_config_policy},
)

mounts_payload = request("GET", "/v1/sys/mounts")
mounts = mounts_payload.get("data", mounts_payload)
if not isinstance(mounts, dict):
    raise SystemExit("OpenBao returned invalid mounts list")
otp_mount = mounts.get("ssh-otp/")
if otp_mount is None:
    request(
        "POST",
        "/v1/sys/mounts/ssh-otp",
        {
            "type": "ssh",
            "description": "Одноразовый SSH-доступ между управляемыми гостями",
        },
    )
elif not isinstance(otp_mount, dict) or otp_mount.get("type") != "ssh":
    raise SystemExit("ssh-otp exists with unexpected type")

auth_payload = request("GET", "/v1/sys/auth")
auth_methods = auth_payload.get("data", auth_payload)
if not isinstance(auth_methods, dict):
    raise SystemExit("OpenBao returned invalid auth methods list")
existing = auth_methods.get("infra-manager/")
if existing is None:
    request(
        "POST",
        "/v1/sys/auth/infra-manager",
        {
            "type": "approle",
            "description": "Служебный доступ infra-manager к SSH-сертификатам",
        },
    )
elif not isinstance(existing, dict) or existing.get("type") != "approle":
    raise SystemExit("auth/infra-manager exists with unexpected type")

machine_auth = auth_methods.get("machine/")
if machine_auth is None:
    request(
        "POST",
        "/v1/sys/auth/machine",
        {
            "type": "approle",
            "description": "Машинная идентичность для SSH OTP",
        },
    )
elif not isinstance(machine_auth, dict) or machine_auth.get("type") != "approle":
    raise SystemExit("auth/machine exists with unexpected type")

auth_payload = request("GET", "/v1/sys/auth")
auth_methods = auth_payload.get("data", auth_payload)
machine_auth = auth_methods.get("machine/") if isinstance(auth_methods, dict) else None
machine_accessor = (
    machine_auth.get("accessor") if isinstance(machine_auth, dict) else None
)
if not isinstance(machine_accessor, str) or not machine_accessor:
    raise SystemExit("auth/machine does not expose an accessor")

machine_policy_path = (
    "ssh-otp/creds/"
    "{{identity.entity.aliases."
    + machine_accessor
    + ".metadata.role_name}}"
)
machine_policy = json.dumps(
    {
        "path": {
            machine_policy_path: {
                "capabilities": ["create", "update"],
            },
            "auth/token/lookup-self": {"capabilities": ["read"]},
            "auth/token/revoke-self": {"capabilities": ["update"]},
        }
    },
    separators=(",", ":"),
)
request(
    "POST",
    "/v1/sys/policies/acl/machine-ssh-otp",
    {"policy": machine_policy},
)

policies_payload = request("LIST", "/v1/sys/policies/acl")
policy_names = policies_payload.get("data", {}).get("keys", [])
if isinstance(policy_names, list):
    for policy_name in policy_names:
        if (
            isinstance(policy_name, str)
            and policy_name.startswith("machine-guest-")
            and policy_name.endswith("-ssh-otp")
        ):
            request("DELETE", f"/v1/sys/policies/acl/{policy_name}")

roles = (
    (
        "ssh-ca-config",
        "infra-manager-ssh-ca-config",
        "10m",
        "15m",
    ),
    (
        "ssh-signer",
        "infra-manager-ssh-signer",
        "5m",
        "10m",
    ),
    (
        "ssh-otp-config",
        "infra-manager-ssh-otp-config",
        "5m",
        "10m",
    ),
)

credentials = {}
for role_name, policy_name, token_ttl, token_max_ttl in roles:
    request(
        "POST",
        f"/v1/auth/infra-manager/role/{role_name}",
        {
            "bind_secret_id": True,
            "secret_id_bound_cidrs": ["127.0.0.1/32"],
            "secret_id_num_uses": 0,
            "secret_id_ttl": "0s",
            "token_bound_cidrs": ["127.0.0.1/32"],
            "token_num_uses": 0,
            "token_policies": [policy_name],
            "token_ttl": token_ttl,
            "token_max_ttl": token_max_ttl,
        },
    )
    role_payload = request(
        "GET",
        f"/v1/auth/infra-manager/role/{role_name}/role-id",
    )
    role_id = role_payload.get("data", {}).get("role_id")
    if not isinstance(role_id, str) or not role_id:
        raise SystemExit(f"OpenBao did not return RoleID for {role_name}")
    existing_item = existing_credentials.get(role_name)
    secret_id = None
    if isinstance(existing_item, dict):
        existing_role_id = existing_item.get("role_id")
        existing_secret_id = existing_item.get("secret_id")
        if (
            existing_role_id == role_id
            and isinstance(existing_secret_id, str)
            and existing_secret_id
        ):
            secret_id = existing_secret_id
    if secret_id is None:
        secret_payload = request(
            "POST",
            f"/v1/auth/infra-manager/role/{role_name}/secret-id",
            {},
        )
        secret_id = secret_payload.get("data", {}).get("secret_id")
    if not isinstance(secret_id, str) or not secret_id:
        raise SystemExit(f"OpenBao did not return SecretID for {role_name}")
    credentials[role_name] = {
        "role_id": role_id,
        "secret_id": secret_id,
    }

print(json.dumps(credentials, separators=(",", ":")))
"""

CONFIGURE_CLIENT_SIGNING_ROLE_CODE = r"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
credentials = json.loads(sys.stdin.read())
role_id = credentials.get("role_id") if isinstance(credentials, dict) else None
secret_id = credentials.get("secret_id") if isinstance(credentials, dict) else None
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, payload=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return config token")

try:
    request(
        "POST",
        "/v1/ssh-client-signer/roles/infra-manager",
        {
            "key_type": "ca",
            "algorithm_signer": "rsa-sha2-256",
            "allow_user_certificates": True,
            "allow_host_certificates": False,
            "allowed_users": "root",
            "default_user": "root",
            "allowed_user_key_lengths": {"ed25519": 0},
            "key_id_format": "infra-manager-ansible-{{public_key_hash}}",
            "default_extensions": {
                "permit-pty": "",
            },
            "ttl": "15m",
        },
        token=token,
    )
    role = request(
        "GET",
        "/v1/ssh-client-signer/roles/infra-manager",
        token=token,
    ).get("data")
    if not isinstance(role, dict):
        raise SystemExit("OpenBao did not return client signing role")
    if role.get("key_type") != "ca":
        raise SystemExit("client signing role has unexpected key_type")
    if role.get("allow_user_certificates") is not True:
        raise SystemExit("client signing role does not allow user certificates")
    if role.get("allow_host_certificates") is True:
        raise SystemExit("client signing role unexpectedly allows host certificates")
    if role.get("default_user") != "root":
        raise SystemExit("client signing role has unexpected default_user")
    if role.get("default_extensions") != {"permit-pty": ""}:
        raise SystemExit("client signing role has unexpected default_extensions")
    key_lengths = role.get("allowed_user_key_lengths")
    if not isinstance(key_lengths, dict) or "ed25519" not in key_lengths:
        raise SystemExit("client signing role does not restrict keys to Ed25519")
    if role.get("key_id_format") != "infra-manager-ansible-{{public_key_hash}}":
        raise SystemExit("client signing role has unexpected key_id_format")
    print(json.dumps({"ready": True}, separators=(",", ":")))
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        token=token,
    )
"""


SIGN_CLIENT_KEY_CODE = r"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid signing payload")
credentials = payload.get("credentials")
public_key = payload.get("public_key")
if not isinstance(credentials, dict):
    raise SystemExit("missing signer credentials")
if not isinstance(public_key, str) or not public_key.startswith("ssh-ed25519 "):
    raise SystemExit("invalid SSH public key")
role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return signer token")

try:
    signed = request(
        "POST",
        "/v1/ssh-client-signer/sign/infra-manager",
        {
            "public_key": public_key,
            "valid_principals": "root",
            "ttl": "15m",
        },
        token=token,
    )
    data = signed.get("data")
    certificate = data.get("signed_key") if isinstance(data, dict) else None
    if (
        not isinstance(certificate, str)
        or not certificate.startswith("ssh-ed25519-cert-v01@openssh.com ")
    ):
        raise SystemExit("OpenBao did not return an Ed25519 SSH certificate")
    print(certificate.strip())
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        token=token,
    )
"""


CONFIGURE_MACHINE_SIGNING_ROLES_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
credentials = payload.get("credentials") if isinstance(payload, dict) else None
vmids = payload.get("vmids") if isinstance(payload, dict) else None
if not isinstance(credentials, dict):
    raise SystemExit("missing config credentials")
if (
    not isinstance(vmids, list)
    or any(not isinstance(vmid, int) or vmid <= 0 for vmid in vmids)
):
    raise SystemExit("invalid machine VMIDs")
vmids = sorted(set(vmids))
role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return config token")

try:
    expected = {f"machine-{vmid}" for vmid in vmids}
    listed = request(
        "LIST",
        "/v1/ssh-client-signer/roles",
        token=token,
    ).get("data", {}).get("keys", [])
    if not isinstance(listed, list):
        listed = []
    for role_name in listed:
        if (
            isinstance(role_name, str)
            and role_name.startswith("machine-")
            and role_name not in expected
        ):
            request(
                "DELETE",
                f"/v1/ssh-client-signer/roles/{role_name}",
                token=token,
            )

    for vmid in vmids:
        role_name = f"machine-{vmid}"
        principal = f"guest-{vmid}"
        request(
            "POST",
            f"/v1/ssh-client-signer/roles/{role_name}",
            {
                "key_type": "ca",
                "algorithm_signer": "rsa-sha2-256",
                "allow_user_certificates": True,
                "allow_host_certificates": False,
                "allowed_users": principal,
                "default_user": principal,
                "allowed_user_key_lengths": {"ed25519": 0},
                "key_id_format": (
                    f"project-machine-{vmid}-"
                    "{{public_key_hash}}"
                ),
                "default_extensions": {
                    "permit-pty": "",
                },
                "ttl": "2h",
            },
            token=token,
        )
        role = request(
            "GET",
            f"/v1/ssh-client-signer/roles/{role_name}",
            token=token,
        ).get("data")
        if not isinstance(role, dict):
            raise SystemExit(f"missing machine role {role_name}")
        if role.get("allow_user_certificates") is not True:
            raise SystemExit(f"{role_name} does not allow user certificates")
        if role.get("allow_host_certificates") is True:
            raise SystemExit(f"{role_name} unexpectedly allows host certificates")
        if role.get("default_user") != principal:
            raise SystemExit(f"{role_name} has unexpected principal")
        if role.get("ttl") != 7200:
            raise SystemExit(f"{role_name} has unexpected ttl")

    verified = request(
        "LIST",
        "/v1/ssh-client-signer/roles",
        token=token,
    ).get("data", {}).get("keys", [])
    actual_machine_roles = {
        item
        for item in verified
        if isinstance(item, str) and item.startswith("machine-")
    }
    if actual_machine_roles != expected:
        raise SystemExit(
            "machine roles do not match requested access contract"
        )
    print(json.dumps({"roles": sorted(expected)}, separators=(",", ":")))
finally:
    request("POST", "/v1/auth/token/revoke-self", {}, token=token)
"""


SIGN_MACHINE_KEY_CODE = r"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
credentials = payload.get("credentials") if isinstance(payload, dict) else None
public_key = payload.get("public_key") if isinstance(payload, dict) else None
vmid = payload.get("vmid") if isinstance(payload, dict) else None
if not isinstance(credentials, dict):
    raise SystemExit("missing signer credentials")
if not isinstance(public_key, str) or not public_key.startswith("ssh-ed25519 "):
    raise SystemExit("invalid SSH public key")
if not isinstance(vmid, int) or vmid <= 0:
    raise SystemExit("invalid machine VMID")
role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return signer token")

try:
    principal = f"guest-{vmid}"
    signed = request(
        "POST",
        f"/v1/ssh-client-signer/sign/machine-{vmid}",
        {
            "public_key": public_key,
            "valid_principals": principal,
            "ttl": "2h",
        },
        token=token,
    )
    data = signed.get("data")
    certificate = data.get("signed_key") if isinstance(data, dict) else None
    if (
        not isinstance(certificate, str)
        or not certificate.startswith("ssh-ed25519-cert-v01@openssh.com ")
    ):
        raise SystemExit("OpenBao did not return machine SSH certificate")
    print(certificate.strip())
finally:
    request("POST", "/v1/auth/token/revoke-self", {}, token=token)
"""


CONFIGURE_HOST_SIGNING_ROLE_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
credentials = json.loads(sys.stdin.read())
role_id = credentials.get("role_id") if isinstance(credentials, dict) else None
secret_id = credentials.get("secret_id") if isinstance(credentials, dict) else None
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, payload=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return config token")

try:
    request(
        "POST",
        "/v1/ssh-host-signer/roles/managed-host",
        {
            "key_type": "ca",
            "algorithm_signer": "rsa-sha2-256",
            "allow_user_certificates": False,
            "allow_host_certificates": True,
            "allow_bare_domains": True,
            "allow_subdomains": True,
            "allowed_domains": "*",
            "key_id_format": "infra-manager-host-{{public_key_hash}}",
            "ttl": "720h",
        },
        token=token,
    )
    role = request(
        "GET",
        "/v1/ssh-host-signer/roles/managed-host",
        token=token,
    ).get("data")
    if not isinstance(role, dict):
        raise SystemExit("OpenBao did not return host signing role")
    if role.get("key_type") != "ca":
        raise SystemExit("host signing role has unexpected key_type")
    if role.get("algorithm_signer") != "rsa-sha2-256":
        raise SystemExit("host signing role has unexpected algorithm_signer")
    if role.get("allow_host_certificates") is not True:
        raise SystemExit("host signing role does not allow host certificates")
    if role.get("allow_user_certificates") is True:
        raise SystemExit("host signing role unexpectedly allows user certificates")
    if role.get("allow_bare_domains") is not True:
        raise SystemExit("host signing role does not allow explicit bare principals")
    if role.get("allowed_domains") != "*":
        raise SystemExit("host signing role has unexpected allowed_domains")
    if role.get("allow_subdomains") is not True:
        raise SystemExit("host signing role has unexpected allow_subdomains")
    if role.get("key_id_format") != "infra-manager-host-{{public_key_hash}}":
        raise SystemExit("host signing role has unexpected key_id_format")
    if role.get("ttl") != 2592000:
        raise SystemExit("host signing role has unexpected ttl")
    print(json.dumps({"ready": True}, separators=(",", ":")))
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        token=token,
    )
"""


SIGN_HOST_KEY_CODE = r"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid host signing payload")
credentials = payload.get("credentials")
public_key = payload.get("public_key")
principals = payload.get("principals")
if not isinstance(credentials, dict):
    raise SystemExit("missing signer credentials")
if not isinstance(public_key, str) or not public_key.startswith("ssh-ed25519 "):
    raise SystemExit("invalid SSH host public key")
if (
    not isinstance(principals, list)
    or not principals
    or any(not isinstance(item, str) or not item for item in principals)
):
    raise SystemExit("invalid SSH host principals")
role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return signer token")

try:
    signed = request(
        "POST",
        "/v1/ssh-host-signer/sign/managed-host",
        {
            "cert_type": "host",
            "public_key": public_key,
            "valid_principals": ",".join(principals),
            "ttl": "720h",
        },
        token=token,
    )
    data = signed.get("data")
    certificate = data.get("signed_key") if isinstance(data, dict) else None
    if (
        not isinstance(certificate, str)
        or not certificate.startswith("ssh-ed25519-cert-v01@openssh.com ")
    ):
        raise SystemExit("OpenBao did not return an Ed25519 SSH host certificate")
    print(certificate.strip())
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        token=token,
    )
"""


CHECK_SSH_ACCESS_CODE = r"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
credentials = json.loads(sys.stdin.read())
if not isinstance(credentials, dict):
    raise SystemExit("invalid credentials payload")


def request(method, path, payload=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


checks = (
    (
        "ssh-ca-config",
        "infra-manager-ssh-ca-config",
        {
            "ssh-client-signer/roles/infra-manager": {
                "create", "read", "update", "delete"
            },
            "ssh-client-signer/sign/infra-manager": {"deny"},
        },
    ),
    (
        "ssh-signer",
        "infra-manager-ssh-signer",
        {
            "ssh-client-signer/sign/infra-manager": {"create", "update"},
            "ssh-client-signer/sign/machine-410": {"create", "update"},
            "ssh-client-signer/roles/infra-manager": {"deny"},
        },
    ),
    (
        "ssh-otp-config",
        "infra-manager-ssh-otp-config",
        {
            "sys/mounts": {"read"},
            "sys/auth": {"read"},
            "ssh-otp/roles/guest-410": {"create", "read", "update", "delete"},
            "auth/machine/role/guest-410": {"create", "read", "update", "delete"},
            "ssh-otp/creds/guest-410": {"deny"},
        },
    ),
)

result = {}
for role_name, expected_policy, expected_paths in checks:
    item = credentials.get(role_name)
    if not isinstance(item, dict):
        raise SystemExit(f"missing credentials for {role_name}")
    role_id = item.get("role_id")
    secret_id = item.get("secret_id")
    if not isinstance(role_id, str) or not isinstance(secret_id, str):
        raise SystemExit(f"invalid credentials for {role_name}")

    login = request(
        "POST",
        "/v1/auth/infra-manager/login",
        {"role_id": role_id, "secret_id": secret_id},
    )
    auth = login.get("auth")
    if not isinstance(auth, dict):
        raise SystemExit(f"OpenBao did not authenticate {role_name}")
    client_token = auth.get("client_token")
    policies = auth.get("policies", auth.get("token_policies", []))
    if not isinstance(client_token, str) or not client_token:
        raise SystemExit(f"OpenBao did not return token for {role_name}")
    if not isinstance(policies, list) or expected_policy not in policies:
        raise SystemExit(f"unexpected policies for {role_name}: {policies!r}")
    if "root" in policies:
        raise SystemExit(f"{role_name} unexpectedly has root policy")

    paths = list(expected_paths)
    capabilities = request(
        "POST",
        "/v1/sys/capabilities-self",
        {"paths": paths},
        token=client_token,
    )
    for path, required in expected_paths.items():
        actual = capabilities.get(path)
        if not isinstance(actual, list):
            raise SystemExit(f"missing capabilities for {role_name}: {path}")
        actual_set = set(actual)
        if required == {"deny"}:
            if actual_set != {"deny"}:
                raise SystemExit(
                    f"{role_name} unexpectedly allowed on {path}: {actual!r}"
                )
        elif not required.issubset(actual_set):
            raise SystemExit(
                f"{role_name} lacks capabilities on {path}: {actual!r}"
            )

    request(
        "POST",
        "/v1/auth/token/revoke-self",
        {},
        token=client_token,
    )
    result[role_name] = True

print(json.dumps(result, separators=(",", ":")))
"""



CONFIGURE_OTP_CONTRACT_CODE = r"""
import ipaddress
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid OTP payload")
credentials = payload.get("credentials")
sources = payload.get("sources")
if not isinstance(credentials, dict):
    raise SystemExit("missing OTP config credentials")
if not isinstance(sources, list):
    raise SystemExit("missing OTP sources")
role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def list_keys(path, *, token):
    try:
        payload = request("LIST", path, token=token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    keys = payload.get("data", {}).get("keys", [])
    return keys if isinstance(keys, list) else []


def private_host_cidr(value):
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise SystemExit(f"invalid CIDR: {value!r}") from exc
    if network.version != 4 or network.prefixlen != 32 or not network.is_private:
        raise SystemExit(f"OTP CIDR must be private IPv4 /32: {value!r}")
    return str(network)


normalized = []
seen_vmids = set()
for source in sources:
    if not isinstance(source, dict):
        raise SystemExit("invalid OTP source")
    vmid = source.get("vmid")
    source_cidr = source.get("source_cidr")
    target_cidrs = source.get("target_cidrs")
    if not isinstance(vmid, int) or vmid <= 0 or vmid in seen_vmids:
        raise SystemExit("invalid or duplicate OTP VMID")
    if not isinstance(source_cidr, str):
        raise SystemExit("missing source CIDR")
    if not isinstance(target_cidrs, list) or not target_cidrs:
        raise SystemExit("OTP source must have at least one target")
    normalized_targets = sorted(
        {private_host_cidr(item) for item in target_cidrs if isinstance(item, str)}
    )
    if len(normalized_targets) != len(set(target_cidrs)):
        raise SystemExit("invalid or duplicate OTP target CIDR")
    normalized.append(
        {
            "vmid": vmid,
            "source_cidr": private_host_cidr(source_cidr),
            "target_cidrs": normalized_targets,
        }
    )
    seen_vmids.add(vmid)

login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return OTP config token")

try:
    mounts_payload = request("GET", "/v1/sys/mounts", token=token)
    mounts = mounts_payload.get("data", mounts_payload)
    if not isinstance(mounts, dict):
        raise SystemExit("invalid mounts list")
    existing_mount = mounts.get("ssh-otp/")
    if existing_mount is None:
        raise SystemExit("ssh-otp is not initialized by administrative setup")
    if not isinstance(existing_mount, dict) or existing_mount.get("type") != "ssh":
        raise SystemExit("ssh-otp exists with unexpected type")

    auth_payload = request("GET", "/v1/sys/auth", token=token)
    auth_methods = auth_payload.get("data", auth_payload)
    if not isinstance(auth_methods, dict):
        raise SystemExit("invalid auth methods list")
    existing_auth = auth_methods.get("machine/")
    if existing_auth is None:
        raise SystemExit("auth/machine is not initialized by administrative setup")
    if not isinstance(existing_auth, dict) or existing_auth.get("type") != "approle":
        raise SystemExit("auth/machine exists with unexpected type")

    expected_roles = {f"guest-{item['vmid']}" for item in normalized}
    existing_otp_roles = list_keys(
        "/v1/ssh-otp/roles",
        token=token,
    )
    for role_name in existing_otp_roles:
        if (
            isinstance(role_name, str)
            and role_name.startswith("guest-")
            and role_name not in expected_roles
        ):
            request("DELETE", f"/v1/ssh-otp/roles/{role_name}", token=token)

    existing_machine_roles = list_keys(
        "/v1/auth/machine/role",
        token=token,
    )
    for role_name in existing_machine_roles:
        if (
            isinstance(role_name, str)
            and role_name.startswith("guest-")
            and role_name not in expected_roles
        ):
            request("DELETE", f"/v1/auth/machine/role/{role_name}", token=token)

    for item in normalized:
        vmid = item["vmid"]
        role_name = f"guest-{vmid}"
        request(
            "POST",
            f"/v1/ssh-otp/roles/{role_name}",
            {
                "key_type": "otp",
                "default_user": "root",
                "allowed_users": "root",
                "cidr_list": ",".join(item["target_cidrs"]),
                "port": 22,
            },
            token=token,
        )
        request(
            "POST",
            f"/v1/auth/machine/role/{role_name}",
            {
                "bind_secret_id": True,
                "secret_id_bound_cidrs": [item["source_cidr"]],
                "secret_id_num_uses": 0,
                "secret_id_ttl": "0s",
                "token_bound_cidrs": [item["source_cidr"]],
                "token_num_uses": 0,
                "token_policies": ["machine-ssh-otp"],
                "token_ttl": "5m",
                "token_max_ttl": "10m",
                "token_no_default_policy": True,
            },
            token=token,
        )
        role_id_payload = request(
            "GET",
            f"/v1/auth/machine/role/{role_name}/role-id",
            token=token,
        )
        actual_role_id = role_id_payload.get("data", {}).get("role_id")
        if not isinstance(actual_role_id, str) or not actual_role_id:
            raise SystemExit(f"missing RoleID for {role_name}")

        otp_role = request(
            "GET",
            f"/v1/ssh-otp/roles/{role_name}",
            token=token,
        ).get("data")
        if not isinstance(otp_role, dict) or otp_role.get("key_type") != "otp":
            raise SystemExit(f"invalid OTP role {role_name}")
        if otp_role.get("default_user") != "root":
            raise SystemExit(f"invalid default_user for {role_name}")
        cidr_value = otp_role.get("cidr_list", "")
        if isinstance(cidr_value, str):
            actual_cidrs = {item.strip() for item in cidr_value.split(",") if item.strip()}
        elif isinstance(cidr_value, list):
            actual_cidrs = {str(item) for item in cidr_value}
        else:
            actual_cidrs = set()
        if actual_cidrs != set(item["target_cidrs"]):
            raise SystemExit(f"OTP targets do not match for {role_name}")

    print(
        json.dumps(
            {
                "roles": sorted(expected_roles),
                "policy": "machine-ssh-otp",
            },
            separators=(",", ":"),
        )
    )
finally:
    request("POST", "/v1/auth/token/revoke-self", {}, token=token)
"""


ISSUE_MACHINE_CREDENTIALS_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid machine credential payload")

credentials = payload.get("credentials")
vmid = payload.get("vmid")
if not isinstance(credentials, dict):
    raise SystemExit("missing OTP config credentials")
if not isinstance(vmid, int) or vmid <= 0:
    raise SystemExit("invalid VMID")

role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing service RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing service SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(token, str) or not token:
    raise SystemExit("OpenBao did not return OTP config token")

try:
    role_name = f"guest-{vmid}"
    role_payload = request(
        "GET",
        f"/v1/auth/machine/role/{role_name}/role-id",
        token=token,
    )
    machine_role_id = role_payload.get("data", {}).get("role_id")
    secret_payload = request(
        "POST",
        f"/v1/auth/machine/role/{role_name}/secret-id",
        {},
        token=token,
    )
    machine_secret_id = secret_payload.get("data", {}).get("secret_id")
    if not isinstance(machine_role_id, str) or not machine_role_id:
        raise SystemExit(f"missing RoleID for {role_name}")
    if not isinstance(machine_secret_id, str) or not machine_secret_id:
        raise SystemExit(f"missing SecretID for {role_name}")
    print(
        json.dumps(
            {
                "vmid": vmid,
                "role_id": machine_role_id,
                "secret_id": machine_secret_id,
            },
            separators=(",", ":"),
        )
    )
finally:
    request("POST", "/v1/auth/token/revoke-self", {}, token=token)
"""


CONFIGURE_KV_CODE = r"""
import json
import pathlib
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
MOUNT = "infra-secrets"
token = sys.stdin.read().strip()
if not token:
    raise SystemExit("empty root token")


def request(method, path, payload=None, *, auth_token=token):
    data = None
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["X-Vault-Token"] = auth_token
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def read_env(path):
    result = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


mounts_payload = request("GET", "/v1/sys/mounts")
mounts = mounts_payload.get("data", mounts_payload)
if not isinstance(mounts, dict):
    raise SystemExit("OpenBao returned invalid mounts list")
existing = mounts.get(f"{MOUNT}/")
if existing is None:
    request(
        "POST",
        f"/v1/sys/mounts/{MOUNT}",
        {
            "type": "kv",
            "description": "Рабочие секреты инфраструктуры",
            "options": {"version": "2"},
        },
    )
elif (
    not isinstance(existing, dict)
    or existing.get("type") != "kv"
    or existing.get("options", {}).get("version") != "2"
):
    raise SystemExit(f"{MOUNT} exists with unexpected type or version")

pve = read_env("/run/infra-manager/secrets/pve-api.env")
endpoint = pve.get("PVE_API_URL", "")
token_id = pve.get("PVE_API_TOKEN_ID", "")
token_secret = pve.get("PVE_API_TOKEN_SECRET", "")
if not endpoint or not token_id or not token_secret:
    raise SystemExit("staged PVE API credential is incomplete")

github_key = pathlib.Path(
    "/run/infra-manager/secrets/github_proxmox_repo_ed25519"
).read_text(encoding="utf-8").strip()
if "PRIVATE KEY" not in github_key:
    raise SystemExit("staged Git credential is invalid")

semaphore = read_env("/run/infra-manager/secrets/semaphore-server.env")
admin_password = semaphore.get("SEMAPHORE_ADMIN_PASSWORD", "")
encryption_key = semaphore.get("SEMAPHORE_ACCESS_KEY_ENCRYPTION", "")
timezone = semaphore.get("TZ", "UTC") or "UTC"
api_token = pathlib.Path(
    "/run/infra-manager/secrets/semaphore-api-token"
).read_text(encoding="utf-8").strip()
if not admin_password or not encryption_key or not api_token:
    raise SystemExit("staged Semaphore credentials are incomplete")

secrets = {
    "pve/api/infra-manager": {
        "endpoint": endpoint,
        "token_id": token_id,
        "token_secret": token_secret,
    },
    "git/github/proxmox-read": {
        "private_key": github_key,
    },
    "services/semaphore": {
        "admin_password": admin_password,
        "access_key_encryption": encryption_key,
        "api_token": api_token,
        "timezone": timezone,
    },
}
for secret_path, data in secrets.items():
    request(
        "POST",
        f"/v1/{MOUNT}/data/{secret_path}",
        {"data": data},
    )

policy = json.dumps(
    {
        "path": {
            f"{MOUNT}/data/pve/api/infra-manager": {
                "capabilities": ["read"],
            },
            f"{MOUNT}/data/git/github/proxmox-read": {
                "capabilities": ["read"],
            },
            f"{MOUNT}/data/services/semaphore": {
                "capabilities": ["read"],
            },
            "auth/token/lookup-self": {
                "capabilities": ["read"],
            },
            "auth/token/revoke-self": {
                "capabilities": ["update"],
            },
            "sys/capabilities-self": {
                "capabilities": ["update"],
            },
        }
    },
    separators=(",", ":"),
)
request(
    "POST",
    "/v1/sys/policies/acl/infra-manager-kv-read",
    {"policy": policy},
)

writer_policy = json.dumps(
    {
        "path": {
            f"{MOUNT}/data/services/semaphore": {
                "capabilities": ["read", "update"],
            },
            "auth/token/lookup-self": {
                "capabilities": ["read"],
            },
            "auth/token/revoke-self": {
                "capabilities": ["update"],
            },
            "sys/capabilities-self": {
                "capabilities": ["update"],
            },
        }
    },
    separators=(",", ":"),
)
request(
    "POST",
    "/v1/sys/policies/acl/infra-manager-kv-semaphore-write",
    {"policy": writer_policy},
)

auth_payload = request("GET", "/v1/sys/auth")
auth_methods = auth_payload.get("data", auth_payload)
if not isinstance(auth_methods, dict):
    raise SystemExit("OpenBao returned invalid auth methods list")
existing_auth = auth_methods.get("infra-manager/")
if existing_auth is None:
    request(
        "POST",
        "/v1/sys/auth/infra-manager",
        {
            "type": "approle",
            "description": "Служебный доступ infra-manager",
        },
    )
elif not isinstance(existing_auth, dict) or existing_auth.get("type") != "approle":
    raise SystemExit("auth/infra-manager exists with unexpected type")

role_definitions = (
    ("kv-reader", "infra-manager-kv-read"),
    ("kv-semaphore-writer", "infra-manager-kv-semaphore-write"),
)
credentials = {}
for role_name, policy_name in role_definitions:
    request(
        "POST",
        f"/v1/auth/infra-manager/role/{role_name}",
        {
            "bind_secret_id": True,
            "secret_id_bound_cidrs": ["127.0.0.1/32"],
            "secret_id_num_uses": 0,
            "secret_id_ttl": "0s",
            "token_bound_cidrs": ["127.0.0.1/32"],
            "token_num_uses": 0,
            "token_policies": [policy_name],
            "token_ttl": "5m",
            "token_max_ttl": "10m",
        },
    )
    role_payload = request(
        "GET",
        f"/v1/auth/infra-manager/role/{role_name}/role-id",
    )
    secret_payload = request(
        "POST",
        f"/v1/auth/infra-manager/role/{role_name}/secret-id",
        {},
    )
    role_id = role_payload.get("data", {}).get("role_id")
    secret_id = secret_payload.get("data", {}).get("secret_id")
    if not isinstance(role_id, str) or not role_id:
        raise SystemExit(f"OpenBao did not return RoleID for {role_name}")
    if not isinstance(secret_id, str) or not secret_id:
        raise SystemExit(f"OpenBao did not return SecretID for {role_name}")
    credentials[role_name] = {
        "role_id": role_id,
        "secret_id": secret_id,
    }

print(json.dumps(credentials, separators=(",", ":")))
"""


CHECK_KV_ACCESS_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
MOUNT = "infra-secrets"
payload = json.loads(sys.stdin.read())
item = payload.get("kv-reader") if isinstance(payload, dict) else None
if not isinstance(item, dict):
    raise SystemExit("missing kv-reader credentials")
role_id = item.get("role_id")
secret_id = item.get("secret_id")
if not isinstance(role_id, str) or not isinstance(secret_id, str):
    raise SystemExit("invalid kv-reader credentials")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
client_token = auth.get("client_token") if isinstance(auth, dict) else None
policies = auth.get("policies", []) if isinstance(auth, dict) else []
if not isinstance(client_token, str) or not client_token:
    raise SystemExit("OpenBao did not authenticate kv-reader")
if "infra-manager-kv-read" not in policies or "root" in policies:
    raise SystemExit("kv-reader has unexpected policies")

expected = {
    "pve/api/infra-manager": {"endpoint", "token_id", "token_secret"},
    "git/github/proxmox-read": {"private_key"},
    "services/semaphore": {
        "admin_password",
        "access_key_encryption",
        "api_token",
        "timezone",
    },
}
try:
    for secret_path, fields in expected.items():
        response = request(
            "GET",
            f"/v1/{MOUNT}/data/{secret_path}",
            token=client_token,
        )
        data = response.get("data", {}).get("data")
        if not isinstance(data, dict) or not fields.issubset(data):
            raise SystemExit(f"incomplete KV secret: {secret_path}")

    capabilities = request(
        "POST",
        "/v1/sys/capabilities-self",
        {
            "paths": [
                f"{MOUNT}/data/pve/api/infra-manager",
                f"{MOUNT}/data/git/github/proxmox-read",
                f"{MOUNT}/data/services/semaphore",
                f"{MOUNT}/data/services/forbidden",
            ]
        },
        token=client_token,
    )
    for allowed in (
        f"{MOUNT}/data/pve/api/infra-manager",
        f"{MOUNT}/data/git/github/proxmox-read",
        f"{MOUNT}/data/services/semaphore",
    ):
        if "read" not in capabilities.get(allowed, []):
            raise SystemExit(f"kv-reader lacks read on {allowed}")
    if capabilities.get(f"{MOUNT}/data/services/forbidden") != ["deny"]:
        raise SystemExit("kv-reader is broader than the allowed KV paths")
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        {},
        token=client_token,
    )

writer = payload.get("kv-semaphore-writer")
if not isinstance(writer, dict):
    raise SystemExit("missing kv-semaphore-writer credentials")
writer_role_id = writer.get("role_id")
writer_secret_id = writer.get("secret_id")
if not isinstance(writer_role_id, str) or not isinstance(writer_secret_id, str):
    raise SystemExit("invalid kv-semaphore-writer credentials")

writer_login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": writer_role_id, "secret_id": writer_secret_id},
)
writer_auth = writer_login.get("auth")
writer_token = (
    writer_auth.get("client_token")
    if isinstance(writer_auth, dict)
    else None
)
writer_policies = (
    writer_auth.get("policies", [])
    if isinstance(writer_auth, dict)
    else []
)
if not isinstance(writer_token, str) or not writer_token:
    raise SystemExit("OpenBao did not authenticate kv-semaphore-writer")
if (
    "infra-manager-kv-semaphore-write" not in writer_policies
    or "root" in writer_policies
):
    raise SystemExit("kv-semaphore-writer has unexpected policies")

try:
    writer_capabilities = request(
        "POST",
        "/v1/sys/capabilities-self",
        {
            "paths": [
                f"{MOUNT}/data/services/semaphore",
                f"{MOUNT}/data/pve/api/infra-manager",
                f"{MOUNT}/data/git/github/proxmox-read",
            ]
        },
        token=writer_token,
    )
    service_caps = set(
        writer_capabilities.get(f"{MOUNT}/data/services/semaphore", [])
    )
    if not {"read", "update"}.issubset(service_caps):
        raise SystemExit("kv-semaphore-writer lacks service update access")
    for forbidden in (
        f"{MOUNT}/data/pve/api/infra-manager",
        f"{MOUNT}/data/git/github/proxmox-read",
    ):
        if writer_capabilities.get(forbidden) != ["deny"]:
            raise SystemExit(
                f"kv-semaphore-writer unexpectedly allowed on {forbidden}"
            )
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        {},
        token=writer_token,
    )

print(json.dumps({"ready": True}, separators=(",", ":")))
"""


MATERIALIZE_RUNTIME_SECRETS_CODE = r"""
import json
import os
import pathlib
import sys
import tempfile
import urllib.request

BASE = "http://127.0.0.1:8200"
MOUNT = "infra-secrets"
TARGET = pathlib.Path("/run/infra-manager/secrets")
RUNTIME_UID = 1001
RUNTIME_GID = 0
payload = json.loads(sys.stdin.read())
item = payload.get("kv-reader") if isinstance(payload, dict) else None
if not isinstance(item, dict):
    raise SystemExit("missing kv-reader credentials")
role_id = item.get("role_id")
secret_id = item.get("secret_id")
if not isinstance(role_id, str) or not isinstance(secret_id, str):
    raise SystemExit("invalid kv-reader credentials")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def read_secret(path, token):
    response = request(
        "GET",
        f"/v1/{MOUNT}/data/{path}",
        token=token,
    )
    data = response.get("data", {}).get("data")
    if not isinstance(data, dict):
        raise SystemExit(f"OpenBao returned invalid secret: {path}")
    return data


def atomic_write(name, value):
    TARGET.mkdir(parents=True, exist_ok=True)
    os.chown(TARGET.parent, 0, 0)
    os.chmod(TARGET.parent, 0o750)
    os.chown(TARGET, 0, 0)
    os.chmod(TARGET, 0o750)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{name}.",
        dir=TARGET,
        text=True,
    )
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchown(fd, RUNTIME_UID, RUNTIME_GID)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            if not value.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, TARGET / name)
        os.chown(TARGET / name, RUNTIME_UID, RUNTIME_GID)
        os.chmod(TARGET / name, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
client_token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(client_token, str) or not client_token:
    raise SystemExit("OpenBao did not authenticate kv-reader")

try:
    pve = read_secret("pve/api/infra-manager", client_token)
    git = read_secret("git/github/proxmox-read", client_token)
    semaphore = read_secret("services/semaphore", client_token)

    endpoint = pve.get("endpoint")
    token_id = pve.get("token_id")
    token_secret = pve.get("token_secret")
    private_key = git.get("private_key")
    admin_password = semaphore.get("admin_password")
    encryption_key = semaphore.get("access_key_encryption")
    api_token = semaphore.get("api_token")
    timezone = semaphore.get("timezone", "UTC")

    values = (
        endpoint,
        token_id,
        token_secret,
        private_key,
        admin_password,
        encryption_key,
        api_token,
        timezone,
    )
    if any(not isinstance(value, str) or not value for value in values):
        raise SystemExit("OpenBao returned incomplete runtime secrets")

    atomic_write(
        "pve-api.env",
        "\n".join(
            [
                f"PVE_API_URL={endpoint}",
                f"PVE_API_TOKEN_ID={token_id}",
                f"PVE_API_TOKEN_SECRET={token_secret}",
            ]
        ),
    )
    atomic_write("github_proxmox_repo_ed25519", private_key)
    atomic_write(
        "semaphore-server.env",
        "\n".join(
            [
                "SEMAPHORE_DB_DIALECT=sqlite",
                "SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite",
                "SEMAPHORE_ADMIN=admin",
                f"SEMAPHORE_ADMIN_PASSWORD={admin_password}",
                "SEMAPHORE_ADMIN_NAME=Admin",
                "SEMAPHORE_ADMIN_EMAIL=admin@localhost",
                f"SEMAPHORE_ACCESS_KEY_ENCRYPTION={encryption_key}",
                f"TZ={timezone}",
            ]
        ),
    )
    atomic_write("initial-admin-password", admin_password)
    atomic_write("semaphore-api-token", api_token)
    atomic_write(".openbao-materialized", "kv-v2")
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        {},
        token=client_token,
    )

print(json.dumps({"ready": True}, separators=(",", ":")))
"""


UPDATE_SEMAPHORE_API_TOKEN_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
MOUNT = "infra-secrets"
payload = json.loads(sys.stdin.read())
credentials = payload.get("credentials") if isinstance(payload, dict) else None
api_token = payload.get("api_token") if isinstance(payload, dict) else None
if not isinstance(credentials, dict):
    raise SystemExit("missing writer credentials")
if (
    not isinstance(api_token, str)
    or not api_token
    or any(char.isspace() for char in api_token)
):
    raise SystemExit("invalid Semaphore API token")

role_id = credentials.get("role_id")
secret_id = credentials.get("secret_id")
if not isinstance(role_id, str) or not role_id:
    raise SystemExit("missing RoleID")
if not isinstance(secret_id, str) or not secret_id:
    raise SystemExit("missing SecretID")


def request(method, path, body=None, *, token=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


login = request(
    "POST",
    "/v1/auth/infra-manager/login",
    {"role_id": role_id, "secret_id": secret_id},
)
auth = login.get("auth")
client_token = auth.get("client_token") if isinstance(auth, dict) else None
if not isinstance(client_token, str) or not client_token:
    raise SystemExit("OpenBao did not authenticate kv-semaphore-writer")

try:
    response = request(
        "GET",
        f"/v1/{MOUNT}/data/services/semaphore",
        token=client_token,
    )
    current = response.get("data", {}).get("data")
    if not isinstance(current, dict):
        raise SystemExit("OpenBao returned invalid Semaphore secret")
    required = {
        "admin_password",
        "access_key_encryption",
        "timezone",
    }
    if not required.issubset(current):
        raise SystemExit("OpenBao Semaphore secret is incomplete")
    if current.get("api_token") != api_token:
        updated = dict(current)
        updated["api_token"] = api_token
        request(
            "POST",
            f"/v1/{MOUNT}/data/services/semaphore",
            {"data": updated},
            token=client_token,
        )
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        {},
        token=client_token,
    )

print(json.dumps({"ready": True}, separators=(",", ":")))
"""


class OpenBaoHostError(RuntimeError):
    pass


def log_detail(message: str) -> None:
    """Показать техническую подробность только в verbose-режиме."""
    if LOG_LEVEL == "verbose":
        print(message)


def log_status(message: str) -> None:
    """Показать рабочее состояние, кроме quiet-режима."""
    if LOG_LEVEL != "quiet":
        print(message)


def run(
    argv: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise OpenBaoHostError(
            f"Команда завершилась с кодом {result.returncode}: "
            f"{' '.join(argv)}{suffix}"
        )
    return result


def pct_exec(
    *args: str,
    capture: bool = False,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["pct", "exec", str(VMID), "--", *args],
        capture=capture,
        check=check,
        input_text=input_text,
    )


def container_running() -> bool:
    result = run(
        ["pct", "status", str(VMID)],
        capture=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().endswith("running")


def read_status(*, wait: bool) -> dict[str, object]:
    attempts = 60 if wait else 1
    last_error = ""

    for _ in range(attempts):
        if container_running():
            result = pct_exec(
                "python3",
                "-c",
                STATUS_CODE,
                capture=True,
                check=False,
            )
            if result.returncode == 0:
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    last_error = "OpenBao вернул некорректный JSON"
                else:
                    if isinstance(payload, dict):
                        initialized = payload.get("initialized")
                        sealed = payload.get("sealed")
                        if isinstance(initialized, bool) and isinstance(sealed, bool):
                            return payload
                    last_error = "OpenBao вернул неполное состояние"
            else:
                last_error = (result.stderr or "").strip()

        if attempts > 1:
            time.sleep(2)

    raise OpenBaoHostError(
        "OpenBao в LXC 910 не стал доступен"
        + (f": {last_error}" if last_error else "")
    )


def _openssl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["openssl", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OpenBaoHostError(
            "OpenSSL завершился ошибкой"
            + (f": {detail}" if detail else "")
        )
    return result


def _public_key_from_certificate(path: Path) -> str:
    return _openssl("x509", "-in", str(path), "-noout", "-pubkey").stdout.strip()


def _public_key_from_private_key(path: Path) -> str:
    return _openssl("pkey", "-in", str(path), "-pubout").stdout.strip()


def _atomic_copy(source: Path, target: Path, *, mode: int, uid: int, gid: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _tls_ca_ready() -> bool:
    if not TLS_CA_KEY.is_file() or not TLS_CA_CERT.is_file():
        return False
    try:
        return (
            _public_key_from_certificate(TLS_CA_CERT)
            == _public_key_from_private_key(TLS_CA_KEY)
        )
    except OpenBaoHostError:
        return False


def _tls_server_ready() -> bool:
    if not TLS_SERVER_KEY.is_file() or not TLS_SERVER_CERT.is_file():
        return False
    verify = _openssl(
        "verify",
        "-CAfile",
        str(TLS_CA_CERT),
        str(TLS_SERVER_CERT),
        check=False,
    )
    if verify.returncode:
        return False
    checkend = _openssl(
        "x509",
        "-in",
        str(TLS_SERVER_CERT),
        "-checkend",
        "86400",
        "-noout",
        check=False,
    )
    if checkend.returncode:
        return False
    details = _openssl(
        "x509",
        "-in",
        str(TLS_SERVER_CERT),
        "-noout",
        "-text",
    ).stdout
    if f"IP Address:{TLS_ADDRESS}" not in details:
        return False
    if f"DNS:{TLS_DNS_NAME}" not in details:
        return False
    try:
        return (
            _public_key_from_certificate(TLS_SERVER_CERT)
            == _public_key_from_private_key(TLS_SERVER_KEY)
        )
    except OpenBaoHostError:
        return False


def _otp_tls_was_initialized() -> bool:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        return False
    try:
        payload = json.loads(SSH_ACCESS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "ssh-otp-config" in payload


def prepare_tls_material() -> None:
    """Подготовить TLS CA на PVE и серверный комплект, доступный LXC 910."""
    TLS_PVE_ONLY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS_PVE_ONLY_DIR, 0o700)
    os.chown(TLS_PVE_ONLY_DIR, 0, 0)

    TLS_ACCESS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS_ACCESS_DIR, 0o755)
    os.chown(TLS_ACCESS_DIR, 100000, 100000)

    ca_parts = [TLS_CA_KEY.exists(), TLS_CA_CERT.exists()]
    if any(ca_parts) and not all(ca_parts):
        raise OpenBaoHostError(
            "TLS CA OpenBao найден частично; автоматическая замена запрещена"
        )

    if not any(ca_parts):
        if _otp_tls_was_initialized():
            raise OpenBaoHostError(
                "TLS CA OpenBao потерян после ввода OTP/TLS-схемы; "
                "автоматическая ротация центра доверия запрещена"
            )
        with tempfile.TemporaryDirectory(
            prefix=".openbao-ca.",
            dir=TLS_PVE_ONLY_DIR,
        ) as temporary_dir:
            temporary = Path(temporary_dir)
            key = temporary / "ca.key"
            cert = temporary / "ca.crt"
            _openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:4096",
                "-nodes",
                "-sha256",
                "-days",
                "3650",
                "-subj",
                "/CN=infra-manager OpenBao TLS CA",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            )
            _atomic_copy(key, TLS_CA_KEY, mode=0o600, uid=0, gid=0)
            _atomic_copy(cert, TLS_CA_CERT, mode=0o644, uid=0, gid=0)

    if not _tls_ca_ready():
        raise OpenBaoHostError(
            "Существующий TLS CA OpenBao повреждён или ключ не соответствует сертификату"
        )

    _atomic_copy(
        TLS_CA_CERT,
        TLS_ACCESS_CA_CERT,
        mode=0o644,
        uid=100000,
        gid=100000,
    )

    if not _tls_server_ready():
        with tempfile.TemporaryDirectory(
            prefix=".openbao-server.",
            dir=TLS_PVE_ONLY_DIR,
        ) as temporary_dir:
            temporary = Path(temporary_dir)
            key = temporary / "server.key"
            csr = temporary / "server.csr"
            cert = temporary / "server.crt"
            extensions = temporary / "server.ext"
            extensions.write_text(
                "[v3_req]\n"
                f"subjectAltName=IP:{TLS_ADDRESS},DNS:{TLS_DNS_NAME}\n"
                "extendedKeyUsage=serverAuth\n"
                "keyUsage=digitalSignature,keyEncipherment\n",
                encoding="utf-8",
            )
            _openssl(
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-sha256",
                "-subj",
                f"/CN={TLS_DNS_NAME}",
                "-keyout",
                str(key),
                "-out",
                str(csr),
            )
            serial = "0x" + os.urandom(16).hex()
            _openssl(
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(TLS_CA_CERT),
                "-CAkey",
                str(TLS_CA_KEY),
                "-set_serial",
                serial,
                "-days",
                "825",
                "-sha256",
                "-extfile",
                str(extensions),
                "-extensions",
                "v3_req",
                "-out",
                str(cert),
            )
            _atomic_copy(key, TLS_SERVER_KEY, mode=0o600, uid=100000, gid=100000)
            _atomic_copy(cert, TLS_SERVER_CERT, mode=0o644, uid=100000, gid=100000)

    if not _tls_server_ready():
        raise OpenBaoHostError("TLS server material OpenBao не прошёл проверку")

    log_detail(
        "[ОК] TLS OpenBao подготовлен: CA остаётся на PVE, серверный комплект доступен 910"
    )


def write_unseal_key(key: str) -> None:
    if not key:
        raise OpenBaoHostError("OpenBao вернул пустой unseal-ключ")

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)

    fd, temporary_name = tempfile.mkstemp(
        prefix=".unseal.",
        dir=KEY_DIR,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(key)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, KEY_PATH)
        os.chmod(KEY_PATH, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_ssh_access_credentials(credentials: dict[str, object]) -> None:
    required = {"ssh-ca-config", "ssh-signer", "ssh-otp-config"}
    if set(credentials) != required:
        raise OpenBaoHostError(
            "OpenBao вернул неполный набор служебных SSH-доступов"
        )

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".ssh-access.",
        dir=KEY_DIR,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                credentials,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SSH_ACCESS_PATH)
        os.chmod(SSH_ACCESS_PATH, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_kv_access_credentials(credentials: dict[str, object]) -> None:
    required = {"kv-reader", "kv-semaphore-writer"}
    if set(credentials) != required:
        raise OpenBaoHostError(
            "OpenBao вернул неполный набор доступа к KV"
        )

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".kv-access.",
        dir=KEY_DIR,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                credentials,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, KV_ACCESS_PATH)
        os.chmod(KV_ACCESS_PATH, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def read_kv_access_credentials() -> dict[str, object]:
    if not KV_ACCESS_PATH.is_file() or KV_ACCESS_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найдены служебные данные доступа к KV: {KV_ACCESS_PATH}"
        )
    try:
        payload = json.loads(KV_ACCESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "Служебные данные доступа к KV содержат некорректный JSON"
        ) from exc
    required = {"kv-reader", "kv-semaphore-writer"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise OpenBaoHostError(
            "Служебные данные доступа к KV имеют некорректный состав"
        )
    return payload


def check_kv_access() -> None:
    credentials = read_kv_access_credentials()
    result = pct_exec(
        "python3",
        "-c",
        CHECK_KV_ACCESS_CODE,
        capture=True,
        input_text=json.dumps(credentials, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат проверки KV"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError("KV v2 не прошёл проверку")


def configure_kv_access(root_token: str) -> None:
    if KV_ACCESS_PATH.exists():
        raise OpenBaoHostError(
            f"Найден старый файл доступа к KV {KV_ACCESS_PATH}; "
            "автоматическая перезапись запрещена"
        )
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_KV_CODE,
        capture=True,
        input_text=root_token,
    )
    try:
        credentials = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа к KV"
        ) from exc
    if not isinstance(credentials, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа к KV"
        )
    write_kv_access_credentials(credentials)
    check_kv_access()
    log_detail("[ОК] KV v2 и ограниченный доступ к рабочим секретам настроены")


def materialize_runtime_secrets() -> None:
    credentials = read_kv_access_credentials()
    result = pct_exec(
        "python3",
        "-c",
        MATERIALIZE_RUNTIME_SECRETS_CODE,
        capture=True,
        input_text=json.dumps(credentials, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат материализации секретов"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "Рабочие секреты не были материализованы из OpenBao"
        )
    log_detail(
        f"[ОК] Рабочие секреты материализованы во временную область {RUNTIME_SECRET_DIR}"
    )


def update_semaphore_api_token(api_token: str) -> None:
    clean = api_token.strip()
    if not clean or any(char.isspace() for char in clean):
        raise OpenBaoHostError("Некорректный API token Semaphore")
    credentials = read_kv_access_credentials()
    writer = credentials.get("kv-semaphore-writer")
    result = pct_exec(
        "python3",
        "-c",
        UPDATE_SEMAPHORE_API_TOKEN_CODE,
        capture=True,
        input_text=json.dumps(
            {
                "credentials": writer,
                "api_token": clean,
            },
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат обновления Semaphore token"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "API token Semaphore не был обновлён в OpenBao"
        )
    log_detail("[ОК] API token Semaphore обновлён в OpenBao")


def unseal() -> None:
    status = read_status(wait=True)
    if not bool(status["initialized"]):
        log_detail("[ИНФО] OpenBao ещё не инициализирован")
        return
    if not bool(status["sealed"]):
        log_status("[ОК] OpenBao уже разблокирован")
        return
    if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"OpenBao инициализирован, но отсутствует ключ: {KEY_PATH}"
        )

    pct_exec("install", "-d", "-m", "0700", str(CT_RUNTIME_DIR))
    run(
        [
            "pct",
            "push",
            str(VMID),
            str(KEY_PATH),
            str(CT_KEY_PATH),
            "--user",
            "0",
            "--group",
            "0",
            "--perms",
            "0600",
        ]
    )
    try:
        result = pct_exec(
            "python3",
            "-c",
            UNSEAL_CODE,
            capture=True,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenBaoHostError(
                "OpenBao вернул некорректный ответ на разблокировку"
            ) from exc
        if not isinstance(payload, dict) or payload.get("sealed") is not False:
            raise OpenBaoHostError("OpenBao остался запечатан после разблокировки")
    finally:
        pct_exec("rm", "-f", str(CT_KEY_PATH), check=False)

    log_status("[ОК] OpenBao разблокирован")


def _revoke_root_token(token: str) -> None:
    if not token:
        raise OpenBaoHostError("OpenBao вернул пустой корневой токен")
    pct_exec(
        "python3",
        "-c",
        REVOKE_ROOT_CODE,
        capture=True,
        input_text=token,
    )


def revoke_initial_root_token(token: str) -> None:
    _revoke_root_token(token)
    log_detail("[ОК] Initial root token отозван")


def revoke_temporary_root_token(token: str) -> None:
    _revoke_root_token(token)
    log_detail("[ОК] Временный корневой токен OpenBao отозван")


def _generate_root_from_running_server() -> str:
    if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найден ключ разблокировки OpenBao: {KEY_PATH}"
        )

    unseal_key = KEY_PATH.read_text(encoding="utf-8").strip()
    result = pct_exec(
        "python3",
        "-c",
        GENERATE_ROOT_CODE,
        capture=True,
        input_text=unseal_key,
    )
    token = result.stdout.strip()
    if not token or any(char.isspace() for char in token):
        raise OpenBaoHostError(
            "OpenBao не вернул корректный временный корневой токен"
        )
    return token


def generate_temporary_root_token() -> str:
    image_result = pct_exec(
        "docker",
        "inspect",
        "-f",
        "{{.Config.Image}}",
        "openbao",
        capture=True,
    )
    image = image_result.stdout.strip()
    if not image:
        raise OpenBaoHostError("Не удалось определить образ OpenBao")

    pct_exec(
        "python3",
        "-c",
        PREPARE_GENERATE_ROOT_CONFIG_CODE,
        capture=True,
    )

    token: str | None = None
    original_stopped = False
    try:
        pct_exec("docker", "stop", "openbao", capture=True)
        original_stopped = True

        pct_exec(
            "docker",
            "rm",
            "-f",
            TEMP_OPENBAO_CONTAINER,
            capture=True,
            check=False,
        )
        pct_exec(
            "docker",
            "run",
            "-d",
            "--name",
            TEMP_OPENBAO_CONTAINER,
            "--network",
            "host",
            "--user",
            "0",
            "-e",
            "BAO_SKIP_DROP_ROOT=1",
            "-e",
            "SKIP_CHOWN=1",
            "-v",
            f"{CT_OPENBAO_DATA_PATH}:/openbao/file",
            "-v",
            "/etc/infra-manager/openbao/tls:/openbao/tls:ro",
            "-v",
            f"{CT_COMPAT_CONFIG_PATH}:/openbao/config/openbao.hcl:ro",
            image,
            "server",
            capture=True,
        )

        unseal()
        token = _generate_root_from_running_server()
    finally:
        pct_exec(
            "docker",
            "rm",
            "-f",
            TEMP_OPENBAO_CONTAINER,
            capture=True,
            check=False,
        )
        pct_exec("rm", "-f", str(CT_COMPAT_CONFIG_PATH), check=False)

        if original_stopped:
            pct_exec("docker", "start", "openbao", capture=True)
            unseal()

    if token is None:
        raise OpenBaoHostError("Временный корневой токен OpenBao не выпущен")

    log_detail("[ОК] Временный корневой токен выпущен в защищённом локальном окне")
    return token


def ssh_cas_ready() -> bool:
    result = pct_exec(
        "python3",
        "-c",
        SSH_CA_STATUS_CODE,
        capture=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректное состояние SSH-центров доверия"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректное состояние SSH-центров доверия"
        )
    return (
        payload.get("client") is True
        and payload.get("host") is True
        and payload.get("distinct") is True
    )


def configure_ssh_cas(token: str) -> None:
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_SSH_CA_CODE,
        capture=True,
        input_text=token,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный ответ настройки SSH-центров доверия"
        ) from exc
    client = payload.get("client_public_key") if isinstance(payload, dict) else None
    host = payload.get("host_public_key") if isinstance(payload, dict) else None
    if (
        not isinstance(client, str)
        or not isinstance(host, str)
        or not client
        or not host
        or client == host
    ):
        raise OpenBaoHostError(
            "OpenBao не подтвердил два независимых SSH-центра доверия"
        )


def ensure_ssh_cas(root_token: str | None = None) -> None:
    if ssh_cas_ready():
        log_detail("[ОК] Два SSH-центра доверия OpenBao уже готовы")
        return

    if root_token is None:
        raise OpenBaoHostError(
            "OpenBao уже инициализирован без SSH-центров доверия. "
            "OpenBao 2.7 не разрешает безопасно выпустить новый root token "
            "только по unseal-ключу; требуется явная миграция или "
            "переинициализация пустого хранилища"
        )

    configure_ssh_cas(root_token)
    if not ssh_cas_ready():
        raise OpenBaoHostError(
            "SSH-центры доверия OpenBao не прошли итоговую проверку"
        )
    log_detail("[ОК] Два SSH-центра доверия OpenBao созданы")


def read_ssh_access_credentials(*, allow_legacy: bool = False) -> dict[str, object]:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найдены служебные данные доступа OpenBao: {SSH_ACCESS_PATH}"
        )
    try:
        payload = json.loads(SSH_ACCESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "Служебные данные доступа OpenBao содержат некорректный JSON"
        ) from exc
    required = {"ssh-ca-config", "ssh-signer", "ssh-otp-config"}
    legacy = {"ssh-ca-config", "ssh-signer"}
    if not isinstance(payload, dict) or (
        set(payload) != required and not (allow_legacy and set(payload) == legacy)
    ):
        raise OpenBaoHostError(
            "Служебные данные доступа OpenBao имеют некорректный состав"
        )
    return payload


def ensure_client_signing_role() -> None:
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-ca-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_CLIENT_SIGNING_ROLE_CODE,
        capture=True,
        input_text=json.dumps(config_access, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат настройки роли подписи"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "Роль подписи клиентских SSH-сертификатов не прошла проверку"
        )
    log_detail("[ОК] Роль подписи SSH-клиента infra-manager настроена")


def sign_client_public_key(public_key: str) -> str:
    normalized = public_key.strip()
    if not normalized.startswith("ssh-ed25519 "):
        raise OpenBaoHostError("Для подписи ожидается открытый ключ Ed25519")

    credentials = read_ssh_access_credentials()
    signer_access = credentials.get("ssh-signer")
    request_payload = json.dumps(
        {
            "credentials": signer_access,
            "public_key": normalized,
        },
        separators=(",", ":"),
    )
    result = pct_exec(
        "python3",
        "-c",
        SIGN_CLIENT_KEY_CODE,
        capture=True,
        input_text=request_payload,
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise OpenBaoHostError(
            "OpenBao вернул некорректный SSH-сертификат клиента"
        )
    return certificate



def sync_otp_contract(sources: list[dict[str, object]]) -> None:
    """Синхронизировать ssh-otp и auth/machine по подготовленному контракту."""
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-otp-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_OTP_CONTRACT_CODE,
        capture=True,
        input_text=json.dumps(
            {"credentials": config_access, "sources": sources},
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат синхронизации SSH OTP"
        ) from exc
    expected_roles = sorted(
        f"guest-{item['vmid']}"
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("vmid"), int)
    )
    if payload != {
        "roles": expected_roles,
        "policy": "machine-ssh-otp",
    }:
        raise OpenBaoHostError(
            "SSH OTP/AppRole не соответствуют переданному access-контракту"
        )
    log_status("[ОК] OpenBao SSH OTP и машинные AppRole синхронизированы")


def issue_machine_credentials(vmid: int) -> dict[str, str]:
    """Выпустить новый SecretID только для одной существующей машинной роли."""
    if not isinstance(vmid, int) or vmid <= 0:
        raise OpenBaoHostError("Некорректный VMID машинной идентичности")
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-otp-config")
    result = pct_exec(
        "python3",
        "-c",
        ISSUE_MACHINE_CREDENTIALS_CODE,
        capture=True,
        input_text=json.dumps(
            {"credentials": config_access, "vmid": vmid},
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные данные машинной идентичности"
        ) from exc
    role_id = payload.get("role_id") if isinstance(payload, dict) else None
    secret_id = payload.get("secret_id") if isinstance(payload, dict) else None
    returned_vmid = payload.get("vmid") if isinstance(payload, dict) else None
    if (
        returned_vmid != vmid
        or not isinstance(role_id, str)
        or not role_id
        or not isinstance(secret_id, str)
        or not secret_id
    ):
        raise OpenBaoHostError(
            "OpenBao не выдал полную машинную идентичность"
        )
    return {"role_id": role_id, "secret_id": secret_id}


def sync_machine_signing_roles(vmids: list[int]) -> None:
    """Синхронизировать OpenBao-роли машинных SSH-идентичностей."""
    clean = sorted(set(vmids))
    if any(not isinstance(vmid, int) or vmid <= 0 for vmid in clean):
        raise OpenBaoHostError("Некорректный список VMID машинных SSH-ролей")
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-ca-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_MACHINE_SIGNING_ROLES_CODE,
        capture=True,
        input_text=json.dumps(
            {"credentials": config_access, "vmids": clean},
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат настройки машинных SSH-ролей"
        ) from exc
    expected = [f"machine-{vmid}" for vmid in clean]
    if payload != {"roles": expected}:
        raise OpenBaoHostError(
            "Машинные SSH-роли OpenBao не прошли итоговую проверку"
        )


def sign_machine_public_key(vmid: int, public_key: str) -> str:
    """Подписать отдельную исходящую машинную SSH-идентичность."""
    normalized = public_key.strip()
    if not isinstance(vmid, int) or vmid <= 0:
        raise OpenBaoHostError("Некорректный VMID машинной SSH-идентичности")
    if not normalized.startswith("ssh-ed25519 "):
        raise OpenBaoHostError(
            "Для машинной подписи ожидается открытый ключ Ed25519"
        )

    credentials = read_ssh_access_credentials()
    signer_access = credentials.get("ssh-signer")
    result = pct_exec(
        "python3",
        "-c",
        SIGN_MACHINE_KEY_CODE,
        capture=True,
        input_text=json.dumps(
            {
                "credentials": signer_access,
                "public_key": normalized,
                "vmid": vmid,
            },
            separators=(",", ":"),
        ),
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise OpenBaoHostError(
            "OpenBao вернул некорректный машинный SSH-сертификат"
        )
    return certificate


def ensure_host_signing_role() -> None:
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-ca-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_HOST_SIGNING_ROLE_CODE,
        capture=True,
        input_text=json.dumps(config_access, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат настройки роли host-подписи"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "Роль подписи SSH-серверов managed-host не прошла проверку"
        )
    log_detail("[ОК] Роль подписи SSH-серверов managed-host настроена")


def validate_managed_host_target(
    vmid: int,
    hostname: str,
    address: str,
) -> list[str]:
    import ipaddress

    if not isinstance(vmid, int) or vmid <= 0:
        raise OpenBaoHostError("Некорректный VMID для host-сертификата")
    clean_hostname = hostname.strip()
    if (
        not clean_hostname
        or "," in clean_hostname
        or any(character.isspace() for character in clean_hostname)
    ):
        raise OpenBaoHostError("Некорректное имя SSH-сервера")
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError as exc:
        raise OpenBaoHostError("Некорректный IP SSH-сервера") from exc
    if ip.version != 4 or not ip.is_private:
        raise OpenBaoHostError(
            "Host-сертификаты разрешены только для частных IPv4-адресов"
        )
    clean_address = str(ip)

    pct = run(
        ["pct", "config", str(vmid)],
        capture=True,
        check=False,
    )
    if pct.returncode == 0:
        config = pct.stdout
        actual_hostname = next(
            (
                line.removeprefix("hostname: ").strip()
                for line in config.splitlines()
                if line.startswith("hostname: ")
            ),
            "",
        )
        if actual_hostname != clean_hostname:
            raise OpenBaoHostError(
                f"LXC {vmid} не подтверждает hostname {clean_hostname}"
            )
        network_line = next(
            (line for line in config.splitlines() if line.startswith("net0: ")),
            "",
        )
        if f"ip={clean_address}/" not in network_line:
            raise OpenBaoHostError(
                f"LXC {vmid} не подтверждает IP {clean_address}"
            )
        return [clean_hostname, clean_address]

    qemu = run(
        ["qm", "config", str(vmid)],
        capture=True,
        check=False,
    )
    if qemu.returncode != 0:
        raise OpenBaoHostError(
            f"VMID {vmid} не найден как управляемый LXC или VM"
        )
    config = qemu.stdout
    actual_name = next(
        (
            line.removeprefix("name: ").strip()
            for line in config.splitlines()
            if line.startswith("name: ")
        ),
        "",
    )
    if actual_name != clean_hostname:
        raise OpenBaoHostError(
            f"VM {vmid} не подтверждает name {clean_hostname}"
        )
    ip_line = next(
        (line for line in config.splitlines() if line.startswith("ipconfig0: ")),
        "",
    )
    if f"ip={clean_address}/" not in ip_line:
        raise OpenBaoHostError(
            f"VM {vmid} не подтверждает IP {clean_address}"
        )
    return [clean_hostname, clean_address]


def sign_host_public_key(public_key: str, principals: list[str]) -> str:
    normalized = public_key.strip()
    if not normalized.startswith("ssh-ed25519 "):
        raise OpenBaoHostError(
            "Для подписи SSH-сервера ожидается открытый ключ Ed25519"
        )
    cleaned = []
    for principal in principals:
        value = principal.strip()
        if (
            not value
            or "," in value
            or any(character.isspace() for character in value)
        ):
            raise OpenBaoHostError("Некорректный principal SSH-сервера")
        cleaned.append(value)
    if not cleaned:
        raise OpenBaoHostError("Не заданы principals SSH-сервера")

    credentials = read_ssh_access_credentials()
    signer_access = credentials.get("ssh-signer")
    request_payload = json.dumps(
        {
            "credentials": signer_access,
            "public_key": normalized,
            "principals": cleaned,
        },
        separators=(",", ":"),
    )
    result = pct_exec(
        "python3",
        "-c",
        SIGN_HOST_KEY_CODE,
        capture=True,
        input_text=request_payload,
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise OpenBaoHostError(
            "OpenBao вернул некорректный SSH-сертификат сервера"
        )
    return certificate

def ssh_access_credentials_complete() -> bool:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        return False
    try:
        read_ssh_access_credentials()
    except OpenBaoHostError:
        return False
    return True


def ssh_access_credentials_current() -> bool:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        return False
    try:
        payload = json.loads(SSH_ACCESS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and set(payload) == {
        "ssh-ca-config",
        "ssh-signer",
        "ssh-otp-config",
    }


def check_ssh_access() -> None:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найдены служебные данные доступа OpenBao: {SSH_ACCESS_PATH}"
        )
    credentials = SSH_ACCESS_PATH.read_text(encoding="utf-8")
    result = pct_exec(
        "python3",
        "-c",
        CHECK_SSH_ACCESS_CODE,
        capture=True,
        input_text=credentials,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат проверки служебного доступа"
        ) from exc
    if payload != {
        "ssh-ca-config": True,
        "ssh-signer": True,
        "ssh-otp-config": True,
    }:
        raise OpenBaoHostError(
            "Служебные доступы OpenBao не прошли проверку"
        )


def configure_ssh_access(root_token: str) -> None:
    existing_credentials: dict[str, object] = {}
    if SSH_ACCESS_PATH.exists():
        existing_credentials = read_ssh_access_credentials(allow_legacy=True)
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_SSH_ACCESS_CODE,
        capture=True,
        input_text=json.dumps(
            {
                "root_token": root_token,
                "existing_credentials": existing_credentials,
            },
            separators=(",", ":"),
        ),
    )
    try:
        credentials = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа"
        ) from exc
    if not isinstance(credentials, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа"
        )
    write_ssh_access_credentials(credentials)
    check_ssh_access()
    log_detail("[ОК] Ограниченный служебный доступ к SSH-центрам настроен")


def client_ca_published() -> bool:
    result = pct_exec(
        "cat",
        str(CT_CLIENT_CA_PATH),
        capture=True,
        check=False,
    )
    key = result.stdout.strip() if result.returncode == 0 else ""
    return key.startswith(("ssh-", "ecdsa-", "sk-"))


def publish_client_ca(root_token: str) -> None:
    result = pct_exec(
        "python3",
        "-c",
        READ_CLIENT_CA_CODE,
        capture=True,
        input_text=root_token,
    )
    key = result.stdout.strip()
    if not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise OpenBaoHostError(
            "OpenBao не вернул корректный открытый ключ центра доступа"
        )

    install_code = r"""
import os
import pathlib
import sys
import tempfile

target = pathlib.Path("/etc/infra-manager/ca/ssh-client-ca.pub")
key = sys.stdin.read().strip()
if not key.startswith(("ssh-", "ecdsa-", "sk-")):
    raise SystemExit("invalid SSH CA public key")

target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(
    prefix=".ssh-client-ca.",
    dir=target.parent,
    text=True,
)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    os.chmod(target, 0o644)
finally:
    temporary.unlink(missing_ok=True)
"""
    pct_exec(
        "python3",
        "-c",
        install_code,
        capture=True,
        input_text=key,
    )
    if not client_ca_published():
        raise OpenBaoHostError(
            "Открытый ключ центра доступа не прошёл проверку после публикации"
        )
    log_detail(
        f"[ОК] Открытый ключ центра доступа опубликован: {CT_CLIENT_CA_PATH}"
    )


def host_ca_published() -> bool:
    result = pct_exec(
        "cat",
        str(CT_HOST_CA_PATH),
        capture=True,
        check=False,
    )
    key = result.stdout.strip() if result.returncode == 0 else ""
    return key.startswith(("ssh-", "ecdsa-", "sk-"))


def publish_host_ca() -> None:
    result = pct_exec(
        "python3",
        "-c",
        READ_HOST_CA_CODE,
        capture=True,
    )
    key = result.stdout.strip()
    if not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise OpenBaoHostError(
            "OpenBao не вернул корректный открытый ключ центра SSH-серверов"
        )

    install_code = r"""
import os
import pathlib
import sys
import tempfile

target = pathlib.Path("/etc/infra-manager/ca/ssh-host-ca.pub")
key = sys.stdin.read().strip()
if not key.startswith(("ssh-", "ecdsa-", "sk-")):
    raise SystemExit("invalid SSH host CA public key")

target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(
    prefix=".ssh-host-ca.",
    dir=target.parent,
    text=True,
)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    os.chmod(target, 0o644)
finally:
    temporary.unlink(missing_ok=True)
"""
    pct_exec(
        "python3",
        "-c",
        install_code,
        capture=True,
        input_text=key,
    )
    if not host_ca_published():
        raise OpenBaoHostError(
            "Открытый ключ центра SSH-серверов не прошёл проверку после публикации"
        )
    log_detail(
        f"[ОК] Открытый ключ центра SSH-серверов опубликован: {CT_HOST_CA_PATH}"
    )


def ensure_existing_openbao_ssh() -> None:
    if (
        ssh_cas_ready()
        and ssh_access_credentials_complete()
        and KV_ACCESS_PATH.is_file()
        and client_ca_published()
    ):
        check_ssh_access()
        check_kv_access()
        materialize_runtime_secrets()
        publish_host_ca()
        ensure_client_signing_role()
        ensure_host_signing_role()
        log_detail("[ОК] SSH-центры, KV v2 и служебные доступы OpenBao уже готовы")
        return

    root_token = generate_temporary_root_token()
    try:
        ensure_ssh_cas(root_token)
        if ssh_access_credentials_complete():
            check_ssh_access()
        else:
            configure_ssh_access(root_token)
        if KV_ACCESS_PATH.is_file():
            check_kv_access()
        else:
            configure_kv_access(root_token)
        materialize_runtime_secrets()
        publish_client_ca(root_token)
        publish_host_ca()
        ensure_client_signing_role()
        ensure_host_signing_role()
    finally:
        revoke_temporary_root_token(root_token)
        del root_token


def initialize() -> None:
    status = read_status(wait=True)
    if bool(status["initialized"]):
        if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
            raise OpenBaoHostError(
                "OpenBao уже инициализирован, но ключ разблокировки "
                "на PVE отсутствует"
            )
        unseal()
        ensure_existing_openbao_ssh()
        log_detail("[ОК] Инициализация OpenBao уже выполнена")
        return

    stale = [
        path
        for path in (KEY_PATH, SSH_ACCESS_PATH, KV_ACCESS_PATH)
        if path.exists()
    ]
    if stale:
        raise OpenBaoHostError(
            "OpenBao не инициализирован, но найдены старые PVE-only данные: "
            + ", ".join(str(path) for path in stale)
            + "; автоматическая перезапись запрещена"
        )

    result = pct_exec(
        "python3",
        "-c",
        INIT_CODE,
        capture=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный ответ инициализации"
        ) from exc

    keys = payload.get("keys_base64")
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
        keys = payload.get("keys")
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
        raise OpenBaoHostError(
            "OpenBao не вернул ровно один ожидаемый unseal-ключ"
        )

    root_token = payload.pop("root_token", None)
    if not isinstance(root_token, str) or not root_token:
        raise OpenBaoHostError("OpenBao не вернул initial root token")

    write_unseal_key(keys[0])
    del keys
    payload.clear()

    unseal()
    try:
        ensure_ssh_cas(root_token)
        configure_ssh_access(root_token)
        configure_kv_access(root_token)
        materialize_runtime_secrets()
        publish_client_ca(root_token)
        publish_host_ca()
        ensure_client_signing_role()
        ensure_host_signing_role()
    finally:
        revoke_initial_root_token(root_token)
        del root_token

    verified = read_status(wait=False)
    if verified.get("initialized") is not True or verified.get("sealed") is not False:
        raise OpenBaoHostError(
            "Состояние OpenBao после инициализации не подтверждено"
        )

    log_detail("[ОК] OpenBao инициализирован: 1 ключ, порог 1")
    log_detail(f"[ОК] Unseal-ключ сохранён только на PVE: {KEY_PATH}")
    log_detail("[ОК] SSH-центры доверия: ssh-client-signer и ssh-host-signer")
    log_detail("[ОК] Служебные AppRole-данные хранятся только на PVE")
    log_detail("[ОК] Рабочие PVE, Git и Semaphore credentials сохранены в KV v2")
    log_detail("[ОК] Initial root token не сохранён и отозван")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Инициализация и разблокировка OpenBao для LXC 910"
    )
    parser.add_argument(
        "--log-level",
        choices=("normal", "verbose", "quiet"),
        default="normal",
        help="Уровень вывода: normal, verbose или quiet",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--initialize",
        action="store_true",
        help="Однократно инициализировать пустой OpenBao перед разблокировкой",
    )
    mode.add_argument(
        "--prepare-tls",
        action="store_true",
        help="Подготовить TLS CA и серверный сертификат OpenBao на PVE",
    )
    mode.add_argument(
        "--sign-client-key",
        action="store_true",
        help="Подписать переданный через stdin открытый ключ SSH-клиента",
    )
    mode.add_argument(
        "--sign-host-key",
        action="store_true",
        help="Подписать переданный через stdin открытый ключ SSH-сервера",
    )
    mode.add_argument(
        "--sync-otp-contract",
        action="store_true",
        help="Синхронизировать ssh-otp и auth/machine из stdin",
    )
    mode.add_argument(
        "--issue-machine-credentials",
        action="store_true",
        help="Выпустить RoleID/SecretID одной машинной AppRole из stdin",
    )
    mode.add_argument(
        "--sync-machine-roles",
        action="store_true",
        help="Синхронизировать машинные SSH-роли OpenBao из stdin",
    )
    mode.add_argument(
        "--sign-machine-key",
        action="store_true",
        help="Подписать машинный SSH-ключ из stdin",
    )
    mode.add_argument(
        "--check-kv",
        action="store_true",
        help="Проверить KV v2 и ограниченный доступ к рабочим секретам",
    )
    mode.add_argument(
        "--update-semaphore-api-token",
        action="store_true",
        help="Обновить API token Semaphore в KV v2 из stdin",
    )
    args = parser.parse_args()

    global LOG_LEVEL
    LOG_LEVEL = args.log_level

    if os.geteuid() != 0:
        raise OpenBaoHostError("Команда должна выполняться от root на PVE")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.prepare_tls:
            prepare_tls_material()
        elif args.initialize:
            prepare_tls_material()
            initialize()
        elif args.sign_client_key:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError("OpenBao запечатан; подпись SSH невозможна")
            public_key = __import__("sys").stdin.read()
            print(sign_client_public_key(public_key))
        elif args.sign_host_key:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError("OpenBao запечатан; подпись SSH невозможна")
            request_payload = json.loads(__import__("sys").stdin.read())
            if not isinstance(request_payload, dict):
                raise OpenBaoHostError("Некорректный запрос подписи SSH-сервера")
            public_key = request_payload.get("public_key")
            vmid = request_payload.get("vmid")
            hostname = request_payload.get("hostname")
            address = request_payload.get("address")
            if (
                not isinstance(public_key, str)
                or not isinstance(vmid, int)
                or not isinstance(hostname, str)
                or not isinstance(address, str)
            ):
                raise OpenBaoHostError("Неполный запрос подписи SSH-сервера")
            principals = validate_managed_host_target(
                vmid,
                hostname,
                address,
            )
            print(sign_host_public_key(public_key, principals))
        elif args.sync_otp_contract:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError(
                    "OpenBao запечатан; синхронизация SSH OTP невозможна"
                )
            payload = json.loads(__import__("sys").stdin.read())
            sources = payload.get("sources") if isinstance(payload, dict) else None
            if not isinstance(sources, list):
                raise OpenBaoHostError("Не передан OTP-контракт")
            sync_otp_contract(sources)
        elif args.issue_machine_credentials:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError(
                    "OpenBao запечатан; выдача машинной идентичности невозможна"
                )
            payload = json.loads(__import__("sys").stdin.read())
            vmid = payload.get("vmid") if isinstance(payload, dict) else None
            if not isinstance(vmid, int) or vmid <= 0:
                raise OpenBaoHostError("Не передан корректный VMID")
            print(
                json.dumps(
                    issue_machine_credentials(vmid),
                    separators=(",", ":"),
                )
            )
        elif args.sync_machine_roles:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError(
                    "OpenBao запечатан; настройка машинных SSH-ролей невозможна"
                )
            payload = json.loads(__import__("sys").stdin.read())
            vmids = payload.get("vmids") if isinstance(payload, dict) else None
            if not isinstance(vmids, list):
                raise OpenBaoHostError("Не передан список VMID машинных SSH-ролей")
            sync_machine_signing_roles(vmids)
            log_status("[ОК] Машинные SSH-роли OpenBao синхронизированы")
        elif args.sign_machine_key:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError(
                    "OpenBao запечатан; машинная SSH-подпись невозможна"
                )
            payload = json.loads(__import__("sys").stdin.read())
            if not isinstance(payload, dict):
                raise OpenBaoHostError("Некорректный запрос машинной SSH-подписи")
            public_key = payload.get("public_key")
            vmid = payload.get("vmid")
            if not isinstance(public_key, str) or not isinstance(vmid, int):
                raise OpenBaoHostError("Неполный запрос машинной SSH-подписи")
            print(sign_machine_public_key(vmid, public_key))
        elif args.check_kv:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError("OpenBao запечатан; проверка KV невозможна")
            check_kv_access()
            log_status("[ОК] KV v2 и ограниченные AppRole подтверждены")
        elif args.update_semaphore_api_token:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError(
                    "OpenBao запечатан; обновление Semaphore token невозможно"
                )
            update_semaphore_api_token(__import__("sys").stdin.read())
        else:
            unseal()
            status = read_status(wait=False)
            if (
                status.get("initialized") is True
                and status.get("sealed") is False
                and KV_ACCESS_PATH.is_file()
            ):
                materialize_runtime_secrets()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpenBaoHostError as exc:
        print(f"ОШИБКА: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
