"""Встроенные программы, выполняемые внутри infra-manager/OpenBao."""

HEALTH_CODE = r"""
import json
import urllib.error
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/sys/health",
    method="GET",
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        status = response.status
        raw = response.read()
except urllib.error.HTTPError as exc:
    status = exc.code
    raw = exc.read()

try:
    body = json.loads(raw.decode("utf-8")) if raw else {}
except json.JSONDecodeError:
    body = {}

print(
    json.dumps(
        {"http_status": status, "body": body},
        separators=(",", ":"),
    )
)
"""


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
rotate_secret_ids = payload.get("rotate_secret_ids", False)
if not isinstance(token, str) or not token:
    raise SystemExit("empty root token")
if not isinstance(existing_credentials, dict):
    raise SystemExit("invalid existing credentials")
if not isinstance(rotate_secret_ids, bool):
    raise SystemExit("invalid rotate_secret_ids")


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
            "auth/machine/role/+": {
                "capabilities": ["create", "update", "delete"],
                "required_parameters": [
                    "secret_id_bound_cidrs",
                    "token_bound_cidrs",
                    "token_policies",
                    "token_ttl",
                    "token_max_ttl",
                    "token_no_default_policy",
                ],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                    "secret_id_bound_cidrs": [],
                    "token_bound_cidrs": [],
                    "token_policies": ["machine-ssh-otp"],
                    "token_ttl": ["5m"],
                    "token_max_ttl": ["10m"],
                    "token_no_default_policy": ["true"],
                },
            },
            "auth/machine/role/+/bind-secret-id": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/secret-id-bound-cidrs": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/secret-id-num-uses": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/secret-id-ttl": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/token-bound-cidrs": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/token-num-uses": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/token-ttl": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/token-max-ttl": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/policies": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/role-id": {
                "capabilities": ["read"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
            },
            "auth/machine/role/+/secret-id": {
                "capabilities": ["create", "update"],
                "allowed_parameters": {
                    "role_name": ["guest-*"],
                },
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
    if not rotate_secret_ids and isinstance(existing_item, dict):
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
            "auth/machine/role/guest-410": {"create", "update", "delete"},
            "auth/machine/role/guest-410/role-id": {"read"},
            "auth/machine/role/guest-410/token-ttl": {"read"},
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

    if role_name == "ssh-otp-config":
        mounts_payload = request(
            "GET",
            "/v1/sys/mounts",
            token=client_token,
        )
        mounts = mounts_payload.get("data", mounts_payload)
        otp_mount = (
            mounts.get("ssh-otp/")
            if isinstance(mounts, dict)
            else None
        )
        if (
            not isinstance(otp_mount, dict)
            or otp_mount.get("type") != "ssh"
        ):
            raise SystemExit("ssh-otp mount is missing or invalid")

        auth_payload = request(
            "GET",
            "/v1/sys/auth",
            token=client_token,
        )
        auth_methods = auth_payload.get("data", auth_payload)
        machine_auth = (
            auth_methods.get("machine/")
            if isinstance(auth_methods, dict)
            else None
        )
        if (
            not isinstance(machine_auth, dict)
            or machine_auth.get("type") != "approle"
        ):
            raise SystemExit("auth/machine is missing or invalid")

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
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if method == "LIST" and exc.code == 404:
            return {}
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise SystemExit(
            f"OpenBao HTTP {exc.code} for {method} {path}: {detail}"
        ) from exc
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
        role_path = f"auth/machine/role/{role_name}"
        capability_payload = request(
            "POST",
            "/v1/sys/capabilities-self",
            {"path": role_path},
            token=token,
        )
        role_capabilities = set(capability_payload.get("capabilities") or [])
        if not {"create", "update"}.issubset(role_capabilities):
            raise SystemExit(
                f"missing AppRole capabilities for {role_path}: "
                f"{sorted(role_capabilities)!r}"
            )

        request(
            "POST",
            f"/v1/auth/machine/role/{role_name}",
            {
                "secret_id_bound_cidrs": [item["source_cidr"]],
                "token_bound_cidrs": [item["source_cidr"]],
                "token_policies": "machine-ssh-otp",
                "token_ttl": "5m",
                "token_max_ttl": "10m",
                "token_no_default_policy": "true",
            },
            token=token,
        )
        def read_role_value(suffix, key):
            payload = request(
                "GET",
                f"/v1/auth/machine/role/{role_name}/{suffix}",
                token=token,
            )
            data = payload.get("data")
            if not isinstance(data, dict) or key not in data:
                raise SystemExit(f"missing {key} for {role_name}")
            return data[key]

        if read_role_value("bind-secret-id", "bind_secret_id") is not True:
            raise SystemExit(f"bind_secret_id is not enabled for {role_name}")
        if read_role_value("secret-id-num-uses", "secret_id_num_uses") != 0:
            raise SystemExit(f"secret_id_num_uses is not unlimited for {role_name}")
        if read_role_value("secret-id-ttl", "secret_id_ttl") != 0:
            raise SystemExit(f"secret_id_ttl is not unlimited for {role_name}")
        if read_role_value("token-num-uses", "token_num_uses") != 0:
            raise SystemExit(f"token_num_uses is not unlimited for {role_name}")
        if read_role_value("token-ttl", "token_ttl") != 300:
            raise SystemExit(f"token_ttl is not 5m for {role_name}")
        if read_role_value("token-max-ttl", "token_max_ttl") != 600:
            raise SystemExit(f"token_max_ttl is not 10m for {role_name}")
        if set(read_role_value("policies", "token_policies") or []) != {
            "machine-ssh-otp"
        }:
            raise SystemExit(f"token policy mismatch for {role_name}")
        def normalized_cidrs(value):
            if isinstance(value, str):
                values = [value]
            elif isinstance(value, list):
                values = value
            else:
                values = []
            result = set()
            for raw in values:
                text = str(raw).strip()
                if not text:
                    continue
                if "/" not in text:
                    text = f"{text}/32"
                result.add(str(ipaddress.ip_network(text, strict=False)))
            return result

        expected_source = {
            str(ipaddress.ip_network(item["source_cidr"], strict=False))
        }
        if normalized_cidrs(
            read_role_value(
                "secret-id-bound-cidrs",
                "secret_id_bound_cidrs",
            )
        ) != expected_source:
            raise SystemExit(f"SecretID CIDR mismatch for {role_name}")
        token_cidrs = read_role_value(
            "token-bound-cidrs",
            "token_bound_cidrs",
        )
        if normalized_cidrs(token_cidrs) != expected_source:
            raise SystemExit(
                f"token CIDR mismatch for {role_name}: {token_cidrs!r}"
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


RECONCILE_KV_ACCESS_CODE = r"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8200"
MOUNT = "infra-secrets"
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


reader_policy = json.dumps(
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
    "/v1/sys/policies/acl/infra-manager-kv-read",
    {"policy": reader_policy},
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


CONFIGURE_OPERATOR_ACCESS_CODE = r"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid operator configuration payload")

token = payload.get("root_token")
username = payload.get("username")
password = payload.get("password")
if not isinstance(token, str) or not token:
    raise SystemExit("empty root token")
if username != "operator":
    raise SystemExit("unexpected operator username")
if not isinstance(password, str) or len(password) < 24:
    raise SystemExit("operator password is too short")


def request(method, path, body=None):
    data = None
    headers = {
        "Content-Type": "application/json",
        "X-Vault-Token": token,
    }
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


operator_policy = json.dumps(
    {
        "path": {
            "infra-secrets/config": {
                "capabilities": ["read"],
            },
            "infra-secrets/data/*": {
                "capabilities": ["create", "read", "update", "delete"],
            },
            "infra-secrets/metadata": {
                "capabilities": ["list"],
            },
            "infra-secrets/metadata/*": {
                "capabilities": ["create", "read", "update", "delete", "list"],
            },
            "infra-secrets/delete/*": {
                "capabilities": ["update"],
            },
            "infra-secrets/undelete/*": {
                "capabilities": ["update"],
            },
            "infra-secrets/destroy/*": {
                "capabilities": ["update"],
            },
            "sys/mounts": {
                "capabilities": ["read"],
            },
            "sys/auth": {
                "capabilities": ["read"],
            },
            "sys/policies/acl": {
                "capabilities": ["list"],
            },
            "sys/policies/acl/*": {
                "capabilities": ["read"],
            },
            "ssh-client-signer/roles": {
                "capabilities": ["list"],
            },
            "ssh-client-signer/roles/*": {
                "capabilities": ["read"],
            },
            "ssh-host-signer/roles": {
                "capabilities": ["list"],
            },
            "ssh-host-signer/roles/*": {
                "capabilities": ["read"],
            },
            "ssh-otp/roles": {
                "capabilities": ["list"],
            },
            "ssh-otp/roles/*": {
                "capabilities": ["read"],
            },
            "auth/token/lookup-self": {
                "capabilities": ["read"],
            },
            "auth/token/renew-self": {
                "capabilities": ["update"],
            },
            "auth/token/revoke-self": {
                "capabilities": ["update"],
            },
            "sys/capabilities-self": {
                "capabilities": ["update"],
            },
            "sys/internal/ui/resultant-acl": {
                "capabilities": ["read"],
            },
        }
    },
    separators=(",", ":"),
)
request(
    "POST",
    "/v1/sys/policies/acl/infra-operator",
    {"policy": operator_policy},
)

auth_payload = request("GET", "/v1/sys/auth")
auth_methods = auth_payload.get("data", auth_payload)
if not isinstance(auth_methods, dict):
    raise SystemExit("OpenBao returned invalid auth methods list")

existing = auth_methods.get("userpass/")
if existing is None:
    request(
        "POST",
        "/v1/sys/auth/userpass",
        {
            "type": "userpass",
            "description": "Вход оператора в OpenBao UI",
        },
    )
elif not isinstance(existing, dict) or existing.get("type") != "userpass":
    raise SystemExit("auth/userpass exists with unexpected type")

request(
    "POST",
    "/v1/sys/auth/userpass/tune",
    {
        "listing_visibility": "unauth",
        "default_lease_ttl": "1h",
        "max_lease_ttl": "8h",
    },
)

escaped_username = urllib.parse.quote(username, safe="")
request(
    "POST",
    f"/v1/auth/userpass/users/{escaped_username}",
    {
        "password": password,
        "token_policies": ["infra-operator"],
        "token_ttl": "1h",
        "token_max_ttl": "8h",
        "token_no_default_policy": True,
    },
)

print(json.dumps({"ready": True}, separators=(",", ":")))
"""


CHECK_OPERATOR_ACCESS_CODE = r"""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8200"
payload = json.loads(sys.stdin.read())
if not isinstance(payload, dict):
    raise SystemExit("invalid operator credentials")

username = payload.get("username")
password = payload.get("password")
if username != "operator":
    raise SystemExit("unexpected operator username")
if not isinstance(password, str) or not password:
    raise SystemExit("missing operator password")


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


mounts_payload = request("GET", "/v1/sys/internal/ui/mounts")
mounts_data = mounts_payload.get("data", mounts_payload)
auth_mounts = mounts_data.get("auth") if isinstance(mounts_data, dict) else None
userpass = auth_mounts.get("userpass/") if isinstance(auth_mounts, dict) else None
if not isinstance(userpass, dict) or userpass.get("type") != "userpass":
    raise SystemExit("userpass is not visible in unauthenticated UI mounts")

escaped_username = urllib.parse.quote(username, safe="")
login = request(
    "POST",
    f"/v1/auth/userpass/login/{escaped_username}",
    {"password": password},
)
auth = login.get("auth")
client_token = auth.get("client_token") if isinstance(auth, dict) else None
policies = auth.get("token_policies") if isinstance(auth, dict) else None
if not isinstance(client_token, str) or not client_token:
    raise SystemExit("OpenBao did not authenticate operator")
if policies != ["infra-operator"]:
    raise SystemExit("operator token has unexpected policies")

try:
    request(
        "GET",
        "/v1/auth/token/lookup-self",
        token=client_token,
    )
    capabilities = request(
        "POST",
        "/v1/sys/capabilities-self",
        {
            "paths": [
                "infra-secrets/data/services/semaphore",
                "sys/auth",
                "sys/policies/acl",
                "ssh-client-signer/roles",
                "sys/storage/raft/configuration",
                "sys/init",
            ]
        },
        token=client_token,
    ).get("data", {})
    if not isinstance(capabilities, dict):
        raise SystemExit("OpenBao returned invalid operator capabilities")

    expected = {
        "infra-secrets/data/services/semaphore": {
            "create", "read", "update", "delete"
        },
        "sys/auth": {"read"},
        "sys/policies/acl": {"list"},
        "ssh-client-signer/roles": {"list"},
    }
    for path, required in expected.items():
        actual = capabilities.get(path)
        if not isinstance(actual, list) or not required.issubset(set(actual)):
            raise SystemExit(f"operator lacks required capabilities on {path}")

    for forbidden in (
        "sys/storage/raft/configuration",
        "sys/init",
    ):
        actual = capabilities.get(forbidden)
        if isinstance(actual, list) and any(item != "deny" for item in actual):
            raise SystemExit(f"operator unexpectedly has access to {forbidden}")
finally:
    request(
        "POST",
        "/v1/auth/token/revoke-self",
        {},
        token=client_token,
    )

print(json.dumps({"ready": True}, separators=(",", ":")))
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
