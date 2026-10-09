"""Ограниченная OpenBao identity для агента PVE.

Код выполняется внутри OpenBao LXC по доверенному вызову с PVE;
запросы идут к локальному HTTP listener, токен временный и не журналируется.
"""

CONFIGURE_AGENT_CODE = r"""
import ipaddress
import json
import sys
import urllib.request

payload = json.loads(sys.stdin.read())
root_token = payload.get("token")
vmid = payload.get("vmid")
cidr = payload.get("source_cidr")
if not isinstance(root_token, str) or not root_token:
    raise SystemExit("missing admin token")
if type(vmid) is not int or vmid <= 0:
    raise SystemExit("invalid agent VMID")
try:
    ipaddress.IPv4Network(cidr, strict=True)
    if ipaddress.ip_network(cidr).prefixlen != 32:
        raise ValueError()
except (TypeError, ValueError):
    raise SystemExit("source must be exact IPv4 /32")

role = f"pve-agent-{vmid}"
policy_name = "pve-agent-ssh-sign"
sign_role = "pve-agent"

def request(method, path, body=None):
    headers = {"X-Vault-Token": root_token, "Content-Type": "application/json"}
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:8200/v1/" + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        raw = res.read()
    return json.loads(raw.decode()) if raw else {}

request("POST", f"sys/policies/acl/{policy_name}", {
    "policy": json.dumps({"path": {
        f"ssh-client-signer/sign/{sign_role}": {"capabilities": ["create", "update"]},
        "auth/token/lookup-self": {"capabilities": ["read"]},
        "auth/token/revoke-self": {"capabilities": ["update"]}
    }}, separators=(",", ":"))
})
request("POST", f"ssh-client-signer/roles/{sign_role}", {
    "key_type": "ca",
    "algorithm_signer": "rsa-sha2-256",
    "allow_user_certificates": True,
    "allow_host_certificates": False,
    "allowed_users": "infra-agent",
    "default_user": "infra-agent",
    "allowed_user_key_lengths": {"ed25519": 0},
    "key_id_format": "pve-agent-{{public_key_hash}}",
    "default_extensions": {},
    "ttl": "2m"
})
request("POST", f"auth/machine/role/{role}", {
    "bind_secret_id": True,
    "secret_id_bound_cidrs": [cidr],
    "token_bound_cidrs": [cidr],
    "token_policies": [policy_name],
    "token_ttl": "2m",
    "token_max_ttl": "5m",
    "token_no_default_policy": True,
    "secret_id_num_uses": 0,
    "secret_id_ttl": "0s"
})
role_data = request("GET", f"auth/machine/role/{role}/role-id")
secret_data = request("POST", f"auth/machine/role/{role}/secret-id", {})
rid = role_data.get("data", {}).get("role_id")
sid = secret_data.get("data", {}).get("secret_id")
if not isinstance(rid, str) or not rid or not isinstance(sid, str) or not sid:
    raise SystemExit("OpenBao did not return agent AppRole credentials")
print(json.dumps({"role_id": rid, "secret_id": sid}, separators=(",", ":")))
"""
