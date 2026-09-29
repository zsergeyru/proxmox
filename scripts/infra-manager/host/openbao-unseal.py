#!/usr/bin/env python3
"""Инициализация и разблокировка OpenBao из доверенного PVE-хоста."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

VMID = 910
OPENBAO_URL = "http://127.0.0.1:8200"
KEY_DIR = Path("/mnt/bindmounts/infra-manager/pve-only/openbao")
KEY_PATH = KEY_DIR / "unseal.key"
SSH_ACCESS_PATH = KEY_DIR / "ssh-access.json"
LOCK_PATH = Path("/run/lock/infra-manager-openbao-unseal.lock")
CT_RUNTIME_DIR = Path("/run/infra-manager")
CT_KEY_PATH = CT_RUNTIME_DIR / "openbao-unseal.key"
CT_COMPAT_CONFIG_PATH = CT_RUNTIME_DIR / "openbao-generate-root.hcl"
CT_OPENBAO_CONFIG_PATH = Path("/opt/infra-manager/compose/openbao/openbao.hcl")
CT_OPENBAO_DATA_PATH = Path("/mnt/persistent-state/openbao")
CT_CLIENT_CA_PATH = Path("/etc/infra-manager/ca/ssh-client-ca.pub")
TEMP_OPENBAO_CONTAINER = "infra-manager-openbao-generate-root"

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
token = sys.stdin.read().strip()
if not token:
    raise SystemExit("empty root token")


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
    secret_payload = request(
        "POST",
        f"/v1/auth/infra-manager/role/{role_name}/secret-id",
        {},
    )
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

CONFIGURE_CLIENT_SIGNING_ROLE_CODE = r"""
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
        "/v1/ssh-client-signer/roles/infra-manager",
        {
            "key_type": "ca",
            "algorithm_signer": "rsa-sha2-256",
            "allow_user_certificates": True,
            "allow_host_certificates": False,
            "allowed_users": "root",
            "default_user": "root",
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
    raise SystemExit("OpenBao did not return signer token")

try:
    signed = request(
        "POST",
        "/v1/ssh-client-signer/sign/infra-manager",
        {
            "public_key": public_key,
            "valid_principals": "root",
            "ttl": "15m",
            "key_id": "infra-manager-ansible",
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
            "ssh-client-signer/roles/infra-manager": {"deny"},
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



class OpenBaoHostError(RuntimeError):
    pass


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
    required = {"ssh-ca-config", "ssh-signer"}
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


def unseal() -> None:
    status = read_status(wait=True)
    if not bool(status["initialized"]):
        print("[ИНФО] OpenBao ещё не инициализирован")
        return
    if not bool(status["sealed"]):
        print("[ОК] OpenBao уже разблокирован")
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

    print("[ОК] OpenBao разблокирован")


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
    print("[ОК] Initial root token отозван")


def revoke_temporary_root_token(token: str) -> None:
    _revoke_root_token(token)
    print("[ОК] Временный корневой токен OpenBao отозван")


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
            "-v",
            f"{CT_OPENBAO_DATA_PATH}:/openbao/file",
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

    print("[ОК] Временный корневой токен выпущен в защищённом локальном окне")
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
        print("[ОК] Два SSH-центра доверия OpenBao уже готовы")
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
    print("[ОК] Два SSH-центра доверия OpenBao созданы")


def read_ssh_access_credentials() -> dict[str, object]:
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
    required = {"ssh-ca-config", "ssh-signer"}
    if not isinstance(payload, dict) or set(payload) != required:
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
    print("[ОК] Роль подписи SSH-клиента infra-manager настроена")


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
    if payload != {"ssh-ca-config": True, "ssh-signer": True}:
        raise OpenBaoHostError(
            "Служебные доступы OpenBao не прошли проверку"
        )


def configure_ssh_access(root_token: str) -> None:
    if SSH_ACCESS_PATH.exists():
        raise OpenBaoHostError(
            f"Найден старый файл служебного доступа {SSH_ACCESS_PATH}; "
            "автоматическая перезапись запрещена"
        )
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_SSH_ACCESS_CODE,
        capture=True,
        input_text=root_token,
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
    print("[ОК] Ограниченный служебный доступ к SSH-центрам настроен")


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
    print(f"[ОК] Открытый ключ центра доступа опубликован: {CT_CLIENT_CA_PATH}")


def ensure_existing_openbao_ssh() -> None:
    if ssh_cas_ready() and SSH_ACCESS_PATH.is_file() and client_ca_published():
        check_ssh_access()
        ensure_client_signing_role()
        print("[ОК] SSH-центры, служебный доступ и открытый ключ OpenBao уже готовы")
        return

    root_token = generate_temporary_root_token()
    try:
        ensure_ssh_cas(root_token)
        if SSH_ACCESS_PATH.is_file():
            check_ssh_access()
        else:
            configure_ssh_access(root_token)
        publish_client_ca(root_token)
        ensure_client_signing_role()
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
        print("[ОК] Инициализация OpenBao уже выполнена")
        return

    stale = [path for path in (KEY_PATH, SSH_ACCESS_PATH) if path.exists()]
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
        publish_client_ca(root_token)
        ensure_client_signing_role()
    finally:
        revoke_initial_root_token(root_token)
        del root_token

    verified = read_status(wait=False)
    if verified.get("initialized") is not True or verified.get("sealed") is not False:
        raise OpenBaoHostError(
            "Состояние OpenBao после инициализации не подтверждено"
        )

    print("[ОК] OpenBao инициализирован: 1 ключ, порог 1")
    print(f"[ОК] Unseal-ключ сохранён только на PVE: {KEY_PATH}")
    print("[ОК] SSH-центры доверия: ssh-client-signer и ssh-host-signer")
    print("[ОК] Служебные SSH-доступы хранятся только на PVE")
    print("[ОК] Initial root token не сохранён и отозван")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Инициализация и разблокировка OpenBao для LXC 910"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--initialize",
        action="store_true",
        help="Однократно инициализировать пустой OpenBao перед разблокировкой",
    )
    mode.add_argument(
        "--sign-client-key",
        action="store_true",
        help="Подписать переданный через stdin открытый ключ SSH-клиента",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise OpenBaoHostError("Команда должна выполняться от root на PVE")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.initialize:
            initialize()
        elif args.sign_client_key:
            status = read_status(wait=False)
            if status.get("sealed") is not False:
                raise OpenBaoHostError("OpenBao запечатан; подпись SSH невозможна")
            public_key = __import__("sys").stdin.read()
            print(sign_client_public_key(public_key))
        else:
            unseal()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpenBaoHostError as exc:
        print(f"ОШИБКА: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
