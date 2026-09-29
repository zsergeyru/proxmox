#!/usr/bin/env python3
"""Проверить SSH-центры доверия на настоящем тестовом OpenBao."""

from __future__ import annotations

import importlib.util
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
        run_code(host.CONFIGURE_SSH_ACCESS_CODE, input_text=initial_root)
    )
    access_status = json.loads(
        run_code(
            host.CHECK_SSH_ACCESS_CODE,
            input_text=json.dumps(access, separators=(",", ":")),
        )
    )
    if access_status != {"ssh-ca-config": True, "ssh-signer": True}:
        fail(f"Некорректный служебный доступ OpenBao: {access_status!r}")

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
    print("[ОК] Временный SSH-сертификат OpenBao реально подписан и проверен")
    print("[ОК] SSH host-сертификат OpenBao реально подписан и проверен")


if __name__ == "__main__":
    main()
