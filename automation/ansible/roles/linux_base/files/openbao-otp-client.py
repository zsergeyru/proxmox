#!/usr/bin/env python3
"""Получить одноразовый SSH-пароль OpenBao для разрешённой цели."""

from __future__ import annotations

import argparse
import ipaddress
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("/etc/infra-manager/openbao/machine.env")
REQUIRED_KEYS = {
    "OPENBAO_ADDR",
    "OPENBAO_ROLE_ID",
    "OPENBAO_SECRET_ID",
    "OPENBAO_CA",
    "OPENBAO_SSH_OTP_ROLE",
}


class OtpClientError(RuntimeError):
    """Ошибка безопасного получения SSH OTP."""


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise OtpClientError(f"Не найден файл машинной идентичности: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OtpClientError(f"Некорректная строка в {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        if key in values:
            raise OtpClientError(f"Повторяющийся параметр {key} в {path}")
        values[key] = value.strip()

    missing = sorted(key for key in REQUIRED_KEYS if not values.get(key))
    if missing:
        raise OtpClientError(
            "В machine.env отсутствуют обязательные параметры: "
            + ", ".join(missing)
        )
    return values


def re_fullmatch_guest_role(value: str) -> bool:
    prefix = "guest-"
    suffix = value[len(prefix):] if value.startswith(prefix) else ""
    return bool(suffix) and suffix.isdigit() and not suffix.startswith("0")


def validate_config(values: dict[str, str]) -> tuple[str, str, str, Path, str]:
    address = values["OPENBAO_ADDR"].rstrip("/")
    parsed = urllib.parse.urlparse(address)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise OtpClientError("OPENBAO_ADDR должен быть простым HTTPS-адресом")

    role_id = values["OPENBAO_ROLE_ID"]
    secret_id = values["OPENBAO_SECRET_ID"]
    role = values["OPENBAO_SSH_OTP_ROLE"]
    if not re_fullmatch_guest_role(role):
        raise OtpClientError("OPENBAO_SSH_OTP_ROLE имеет неверный формат")

    ca_path = Path(values["OPENBAO_CA"])
    if not ca_path.is_absolute() or not ca_path.is_file():
        raise OtpClientError(f"Не найден TLS CA OpenBao: {ca_path}")

    return address, role_id, secret_id, ca_path, role


def validate_target(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise OtpClientError("Цель должна быть точным IPv4-адресом") from exc
    if address.version != 4:
        raise OtpClientError("Цель должна быть IPv4-адресом")
    return str(address)


def request_json(
    base_url: str,
    path: str,
    payload: dict[str, object],
    *,
    context: ssl.SSLContext,
    token: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["X-Vault-Token"] = token
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            context=context,
            timeout=10,
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise OtpClientError(
            f"OpenBao отклонил запрос {path}: HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OtpClientError(f"OpenBao недоступен для запроса {path}") from exc

    if not raw:
        return {}
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OtpClientError("OpenBao вернул некорректный JSON") from exc
    if not isinstance(result, dict):
        raise OtpClientError("OpenBao вернул неожиданный ответ")
    return result


def request_otp(
    config_path: Path,
    target: str,
    *,
    username: str = "root",
) -> str:
    if username != "root":
        raise OtpClientError("Проектный OTP-доступ разрешён только пользователю root")

    values = read_env(config_path)
    base_url, role_id, secret_id, ca_path, role = validate_config(values)
    target_ip = validate_target(target)
    context = ssl.create_default_context(cafile=str(ca_path))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    login = request_json(
        base_url,
        "/v1/auth/machine/login",
        {"role_id": role_id, "secret_id": secret_id},
        context=context,
    )
    auth = login.get("auth")
    token = auth.get("client_token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        raise OtpClientError("OpenBao не выдал машинный token")

    try:
        credential = request_json(
            base_url,
            f"/v1/ssh-otp/creds/{role}",
            {"ip": target_ip, "username": username},
            context=context,
            token=token,
        )
        data = credential.get("data")
        if not isinstance(data, dict):
            raise OtpClientError("OpenBao не вернул SSH OTP")

        key = data.get("key")
        returned_ip = data.get("ip")
        returned_user = data.get("username")
        key_type = data.get("key_type")
        port = data.get("port")
        if (
            not isinstance(key, str)
            or not key
            or returned_ip != target_ip
            or returned_user != username
            or key_type != "otp"
            or port != 22
        ):
            raise OtpClientError("OpenBao вернул SSH OTP с неверными параметрами")
    finally:
        request_json(
            base_url,
            "/v1/auth/token/revoke-self",
            {},
            context=context,
            token=token,
        )

    return key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Получить одноразовый SSH-пароль OpenBao для разрешённой цели"
    )
    parser.add_argument("target", help="точный IPv4-адрес SSH-цели")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"machine.env (по умолчанию {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--user",
        default="root",
        help="SSH-пользователь; проектный контракт разрешает только root",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        otp = request_otp(args.config, args.target, username=args.user)
    except OtpClientError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    print(otp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
