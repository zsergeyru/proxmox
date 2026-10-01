#!/usr/bin/env python3
"""Проверить SSH-центры доверия на настоящем тестовом OpenBao."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def fail(message: str) -> None:
    raise SystemExit(message)


def load_host_module():
    path = ROOT / "scripts/infra-manager/host/openbao-unseal.py"
    spec = importlib.util.spec_from_file_location("openbao_host_live", path)
    if spec is None or spec.loader is None:
        fail("Не удалось загрузить сценарий OpenBao")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_code(code: str, *, input_text: str = "") -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        fail(
            "Встроенная проверка OpenBao завершилась ошибкой: "
            + result.stderr.strip()
        )
    return result.stdout.strip()


def http_json(
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    token: str | None = None,
) -> dict[str, object]:
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:8200{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def main() -> None:
    host = load_host_module()

    initialized = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            "BAO_ADDR=http://127.0.0.1:8200",
            "openbao-test",
            "bao",
            "operator",
            "init",
            "-key-shares=1",
            "-key-threshold=1",
            "-format=json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if initialized.returncode:
        fail(
            "Не удалось инициализировать тестовый OpenBao: "
            + initialized.stderr.strip()
        )

    payload = json.loads(initialized.stdout)
    keys = payload.get("unseal_keys_b64")
    if not isinstance(keys, list) or len(keys) != 1:
        keys = payload.get("keys_base64")
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
        fail("Тестовый OpenBao не вернул один ключ разблокировки")
    unseal_key = keys[0]

    initial_root = payload.get("root_token")
    if not isinstance(initial_root, str) or not initial_root:
        fail("Тестовый OpenBao не вернул initial root token")

    unsealed = http_json(
        "POST",
        "/v1/sys/unseal",
        {"key": unseal_key},
    )
    if unsealed.get("sealed") is not False:
        fail("Тестовый OpenBao не разблокирован")

    first = json.loads(
        run_code(host.CONFIGURE_SSH_CA_CODE, input_text=initial_root)
    )
    second = json.loads(
        run_code(host.CONFIGURE_SSH_CA_CODE, input_text=initial_root)
    )
    if first != second:
        fail("Повторная настройка заменила SSH-ключ центра доверия")
    if first.get("client_public_key") == first.get("host_public_key"):
        fail("Два SSH-центра используют один открытый ключ")

    status = json.loads(run_code(host.SSH_CA_STATUS_CODE))
    if status != {"client": True, "host": True, "distinct": True}:
        fail(f"Некорректное состояние SSH-центров: {status!r}")

    access = json.loads(
        run_code(
            host.CONFIGURE_SSH_ACCESS_CODE,
            input_text=json.dumps(
                {
                    "root_token": initial_root,
                    "existing_credentials": {},
                },
                separators=(",", ":"),
            ),
        )
    )
    access_status = json.loads(
        run_code(
            host.CHECK_SSH_ACCESS_CODE,
            input_text=json.dumps(access, separators=(",", ":")),
        )
    )
    if access_status != {
        "ssh-ca-config": True,
        "ssh-signer": True,
        "ssh-otp-config": True,
    }:
        fail(f"Некорректный служебный доступ OpenBao: {access_status!r}")

    otp_contract = json.loads(
        run_code(
            host.CONFIGURE_OTP_CONTRACT_CODE,
            input_text=json.dumps(
                {
                    "credentials": access["ssh-otp-config"],
                    "sources": [
                        {
                            "vmid": 410,
                            "source_cidr": "127.0.0.1/32",
                            "target_cidrs": [
                                "192.168.9.10/32",
                                "192.168.3.11/32",
                            ],
                        },
                        {
                            "vmid": 910,
                            "source_cidr": "127.0.0.1/32",
                            "target_cidrs": ["192.168.4.10/32"],
                        },
                    ],
                },
                separators=(",", ":"),
            ),
        )
    )
    if otp_contract != {
        "roles": ["guest-410", "guest-910"],
        "policy": "machine-ssh-otp",
    }:
        fail(f"SSH OTP/AppRole не готовы: {otp_contract!r}")

    machine_role = http_json(
        "GET",
        "/v1/auth/machine/role/guest-410",
        token=initial_root,
    ).get("data")
    if not isinstance(machine_role, dict):
        fail("Тестовый AppRole guest-410 не читается")
    if machine_role.get("token_policies") != ["machine-ssh-otp"]:
        fail(f"guest-410 получил неверные token_policies: {machine_role!r}")
    if machine_role.get("token_no_default_policy") is not True:
        fail("guest-410 получил default policy")
    if machine_role.get("secret_id_bound_cidrs") != ["127.0.0.1/32"]:
        fail(f"guest-410 имеет неверный SecretID CIDR: {machine_role!r}")
    token_cidrs = machine_role.get("token_bound_cidrs")
    if not isinstance(token_cidrs, list):
        fail(f"guest-410 имеет неверный token CIDR: {machine_role!r}")
    normalized_token_cidrs = {
        str(
            ipaddress.ip_network(
                value if "/" in str(value) else f"{value}/32",
                strict=False,
            )
        )
        for value in token_cidrs
    }
    if normalized_token_cidrs != {"127.0.0.1/32"}:
        fail(f"guest-410 имеет неверный token CIDR: {machine_role!r}")

    if machine_role.get("bind_secret_id") is not True:
        fail("guest-410 не требует SecretID")
    if machine_role.get("token_ttl") != 300:
        fail(f"guest-410 имеет неверный token_ttl: {machine_role!r}")
    if machine_role.get("token_max_ttl") != 600:
        fail(f"guest-410 имеет неверный token_max_ttl: {machine_role!r}")

    otp_config_login = http_json(
        "POST",
        "/v1/auth/infra-manager/login",
        {
            "role_id": access["ssh-otp-config"]["role_id"],
            "secret_id": access["ssh-otp-config"]["secret_id"],
        },
    )
    otp_config_token = otp_config_login.get("auth", {}).get("client_token")
    if not isinstance(otp_config_token, str) or not otp_config_token:
        fail("Служебный ssh-otp-config AppRole не проходит login")

    try:
        http_json(
            "POST",
            "/v1/auth/machine/role/guest-410",
            {
                "bind_secret_id": True,
                "secret_id_bound_cidrs": ["127.0.0.1/32"],
                "secret_id_num_uses": 0,
                "secret_id_ttl": "0s",
                "token_bound_cidrs": ["127.0.0.1/32"],
                "token_num_uses": 0,
                "token_policies": ["root"],
                "token_ttl": "5m",
                "token_max_ttl": "10m",
                "token_no_default_policy": True,
            },
            token=otp_config_token,
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            fail(f"Неверный код запрета опасного AppRole: HTTP {exc.code}")
    else:
        fail("ssh-otp-config смог назначить машинному AppRole root policy")

    role_id = http_json(
        "GET",
        "/v1/auth/machine/role/guest-410/role-id",
        token=initial_root,
    ).get("data", {}).get("role_id")
    secret_id = http_json(
        "POST",
        "/v1/auth/machine/role/guest-410/secret-id",
        {},
        token=initial_root,
    ).get("data", {}).get("secret_id")
    if not isinstance(role_id, str) or not isinstance(secret_id, str):
        fail("Тестовый AppRole guest-410 не выдал RoleID/SecretID")

    machine_login = http_json(
        "POST",
        "/v1/auth/machine/login",
        {"role_id": role_id, "secret_id": secret_id},
    )
    machine_token = machine_login.get("auth", {}).get("client_token")
    if not isinstance(machine_token, str) or not machine_token:
        fail("Машинный AppRole guest-410 не проходит login")

    machine_policies = machine_login.get("auth", {}).get("policies", [])
    if set(machine_policies) != {"machine-ssh-otp"}:
        fail(f"Машинный token получил лишние policy: {machine_policies!r}")

    capabilities = http_json(
        "POST",
        "/v1/sys/capabilities-self",
        {
            "paths": [
                "ssh-otp/creds/guest-410",
                "ssh-otp/creds/guest-910",
                "sys/policies/acl/machine-ssh-otp",
            ]
        },
        token=machine_token,
    )
    own = set(capabilities.get("ssh-otp/creds/guest-410", []))
    other = set(capabilities.get("ssh-otp/creds/guest-910", []))
    policy_access = set(capabilities.get("sys/policies/acl/machine-ssh-otp", []))
    if not {"create", "update"}.issubset(own):
        fail(f"guest-410 не получил свой OTP path: {sorted(own)!r}")
    if other != {"deny"}:
        fail(f"guest-410 получил чужой OTP path: {sorted(other)!r}")
    if policy_access != {"deny"}:
        fail(f"машинный token получил доступ к ACL policy: {sorted(policy_access)!r}")

    config_result = json.loads(
        run_code(
            host.CONFIGURE_CLIENT_SIGNING_ROLE_CODE,
            input_text=json.dumps(
                access["ssh-ca-config"],
                separators=(",", ":"),
            ),
        )
    )
    if config_result != {"ready": True}:
        fail(f"Роль клиентской SSH-подписи не готова: {config_result!r}")

    with tempfile.TemporaryDirectory() as tmp:
        identity = Path(tmp) / "ansible_ed25519"
        generated = subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(identity),
                "-C",
                "infra-manager-live-test",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if generated.returncode:
            fail("Не удалось создать временный SSH-ключ: " + generated.stderr.strip())
        public_key = identity.with_suffix(".pub").read_text(
            encoding="utf-8"
        ).strip()
        certificate = run_code(
            host.SIGN_CLIENT_KEY_CODE,
            input_text=json.dumps(
                {
                    "credentials": access["ssh-signer"],
                    "public_key": public_key,
                },
                separators=(",", ":"),
            ),
        )
        certificate_path = Path(str(identity) + "-cert.pub")
        certificate_path.write_text(certificate + "\n", encoding="utf-8")
        inspected = subprocess.run(
            ["ssh-keygen", "-L", "-f", str(certificate_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if inspected.returncode:
            fail("OpenSSH не принял сертификат OpenBao: " + inspected.stderr.strip())
        if 'Key ID: "infra-manager-ansible-' not in inspected.stdout:
            fail("SSH-сертификат получил неожиданный Key ID")
        if "Principals:" not in inspected.stdout or "root" not in inspected.stdout:
            fail("SSH-сертификат не ограничен principal root")
        if "permit-pty" not in inspected.stdout:
            fail("SSH-сертификат не разрешает PTY для Ansible")

    machine_roles = json.loads(
        run_code(
            host.CONFIGURE_MACHINE_SIGNING_ROLES_CODE,
            input_text=json.dumps(
                {
                    "credentials": access["ssh-ca-config"],
                    "vmids": [410, 910],
                },
                separators=(",", ":"),
            ),
        )
    )
    if machine_roles != {"roles": ["machine-410", "machine-910"]}:
        fail(f"Машинные SSH-роли OpenBao не готовы: {machine_roles!r}")

    with tempfile.TemporaryDirectory() as tmp:
        machine_identity = Path(tmp) / "machine_ed25519"
        generated = subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(machine_identity),
                "-C",
                "machine-410-live-test",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if generated.returncode:
            fail(
                "Не удалось создать тестовый машинный SSH-ключ: "
                + generated.stderr.strip()
            )
        machine_public_key = machine_identity.with_suffix(".pub").read_text(
            encoding="utf-8"
        ).strip()
        machine_certificate = run_code(
            host.SIGN_MACHINE_KEY_CODE,
            input_text=json.dumps(
                {
                    "credentials": access["ssh-signer"],
                    "public_key": machine_public_key,
                    "vmid": 410,
                },
                separators=(",", ":"),
            ),
        )
        machine_certificate_path = Path(
            str(machine_identity) + "-cert.pub"
        )
        machine_certificate_path.write_text(
            machine_certificate + "\n",
            encoding="utf-8",
        )
        inspected_machine = subprocess.run(
            ["ssh-keygen", "-L", "-f", str(machine_certificate_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if inspected_machine.returncode:
            fail(
                "OpenSSH не принял машинный сертификат OpenBao: "
                + inspected_machine.stderr.strip()
            )
        if "guest-410" not in inspected_machine.stdout:
            fail("Машинный сертификат не ограничен principal guest-410")
        if 'Key ID: "project-machine-410-' not in inspected_machine.stdout:
            fail("Машинный сертификат получил неожиданный Key ID")

    reduced_roles = json.loads(
        run_code(
            host.CONFIGURE_MACHINE_SIGNING_ROLES_CODE,
            input_text=json.dumps(
                {
                    "credentials": access["ssh-ca-config"],
                    "vmids": [910],
                },
                separators=(",", ":"),
            ),
        )
    )
    if reduced_roles != {"roles": ["machine-910"]}:
        fail("Удаление машинной SSH-роли не подтверждено")

    host_role_result = json.loads(
        run_code(
            host.CONFIGURE_HOST_SIGNING_ROLE_CODE,
            input_text=json.dumps(
                access["ssh-ca-config"],
                separators=(",", ":"),
            ),
        )
    )
    if host_role_result != {"ready": True}:
        fail(f"Роль подписи SSH-сервера не готова: {host_role_result!r}")

    with tempfile.TemporaryDirectory() as tmp:
        host_identity = Path(tmp) / "ssh_host_ed25519_key"
        generated = subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(host_identity),
                "-C",
                "managed-host-live-test",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if generated.returncode:
            fail(
                "Не удалось создать тестовый host key: "
                + generated.stderr.strip()
            )
        host_public_key = host_identity.with_suffix(".pub").read_text(
            encoding="utf-8"
        ).strip()
        host_certificate = run_code(
            host.SIGN_HOST_KEY_CODE,
            input_text=json.dumps(
                {
                    "credentials": access["ssh-signer"],
                    "public_key": host_public_key,
                    "principals": ["test-host", "192.0.2.10"],
                },
                separators=(",", ":"),
            ),
        )
        host_certificate_path = Path(str(host_identity) + "-cert.pub")
        host_certificate_path.write_text(
            host_certificate + "\n",
            encoding="utf-8",
        )
        inspected_host = subprocess.run(
            ["ssh-keygen", "-L", "-f", str(host_certificate_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if inspected_host.returncode:
            fail(
                "OpenSSH не принял host-сертификат OpenBao: "
                + inspected_host.stderr.strip()
            )
        if "host certificate" not in inspected_host.stdout:
            fail("OpenBao выдал не host-сертификат")
        if "test-host" not in inspected_host.stdout:
            fail("Host-сертификат не содержит имя гостя")
        if "192.0.2.10" not in inspected_host.stdout:
            fail("Host-сертификат не содержит IP гостя")

    revoked_token = initial_root
    run_code(host.REVOKE_ROOT_CODE, input_text=initial_root)
    initial_root = ""

    try:
        http_json(
            "GET",
            "/v1/auth/token/lookup-self",
            token=revoked_token,
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 403}:
            raise
    else:
        fail("Отозванный initial root token остался действующим")

    temporary_root = run_code(
        host.GENERATE_ROOT_CODE,
        input_text=unseal_key,
    )
    lookup = http_json(
        "GET",
        "/v1/auth/token/lookup-self",
        token=temporary_root,
    )
    data = lookup.get("data")
    policies = data.get("policies") if isinstance(data, dict) else None
    if not isinstance(policies, list) or "root" not in policies:
        fail("Временный токен не получил root-политику")

    revoked_temporary_root = temporary_root
    run_code(host.REVOKE_ROOT_CODE, input_text=temporary_root)
    temporary_root = ""

    try:
        http_json(
            "GET",
            "/v1/auth/token/lookup-self",
            token=revoked_temporary_root,
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 403}:
            raise
    else:
        fail("Отозванный временный root-токен остался действующим")

    print("[ОК] Временный root-токен OpenBao выпущен и отозван")
    print("[ОК] Два SSH-центра OpenBao проверены на настоящем сервере")
    print("[ОК] SSH OTP и auth/machine проверены на настоящем OpenBao")
    print("[ОК] Временный SSH-сертификат OpenBao реально подписан и проверен")
    print("[ОК] SSH host-сертификат OpenBao реально подписан и проверен")
    print("[ОК] Машинный SSH-сертификат и отзыв роли OpenBao проверены")


if __name__ == "__main__":
    main()
