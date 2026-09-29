#!/usr/bin/env python3
"""Проверить SSH-центры доверия на настоящем тестовом OpenBao."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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

    print("[ОК] Два SSH-центра OpenBao проверены на настоящем сервере")


if __name__ == "__main__":
    main()
