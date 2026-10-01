#!/usr/bin/env python3
"""Проверить одноразовый SSH-пароль OpenBao на целевом госте."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("/etc/infra-manager/openbao/otp-verify.env")
REQUIRED_KEYS = {
    "OPENBAO_ADDR",
    "OPENBAO_CA",
    "OPENBAO_SSH_MOUNT",
    "OPENBAO_TARGET_IP",
}


class OtpVerifyError(RuntimeError):
    """Ошибка безопасной проверки SSH OTP."""


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise OtpVerifyError(f"Не найден файл настройки OTP: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OtpVerifyError(f"Некорректная строка в {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        if key in values:
            raise OtpVerifyError(f"Повторяющийся параметр {key} в {path}")
        values[key] = value.strip()

    missing = sorted(key for key in REQUIRED_KEYS if not values.get(key))
    if missing:
        raise OtpVerifyError(
            "В настройке OTP отсутствуют обязательные параметры: "
            + ", ".join(missing)
        )
    return values


def validate_config(values: dict[str, str]) -> tuple[str, Path, str, str]:
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
        raise OtpVerifyError("OPENBAO_ADDR должен быть простым HTTPS-адресом")

    ca_path = Path(values["OPENBAO_CA"])
    if not ca_path.is_absolute() or not ca_path.is_file():
        raise OtpVerifyError(f"Не найден TLS CA OpenBao: {ca_path}")

    mount = values["OPENBAO_SSH_MOUNT"]
    if mount != "ssh-otp":
        raise OtpVerifyError("OPENBAO_SSH_MOUNT должен быть ssh-otp")

    try:
        target = ipaddress.ip_address(values["OPENBAO_TARGET_IP"])
    except ValueError as exc:
        raise OtpVerifyError("OPENBAO_TARGET_IP должен быть IPv4-адресом") from exc
    if target.version != 4:
        raise OtpVerifyError("OPENBAO_TARGET_IP должен быть IPv4-адресом")

    return address, ca_path, mount, str(target)


def read_otp(stream: Any) -> str:
    raw = stream.read(1024)
    if not isinstance(raw, str):
        raise OtpVerifyError("PAM не передал OTP как текст")
    otp = raw.strip()
    if not otp or len(otp) > 512 or any(character.isspace() for character in otp):
        raise OtpVerifyError("Получен некорректный SSH OTP")
    return otp


def verify_otp(
    config_path: Path,
    otp: str,
    *,
    username: str,
) -> None:
    if username != "root":
        raise OtpVerifyError("Проектный OTP-доступ разрешён только root")

    values = read_env(config_path)
    base_url, ca_path, mount, expected_target = validate_config(values)

    context = ssl.create_default_context(cafile=str(ca_path))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    request = urllib.request.Request(
        f"{base_url}/v1/{mount}/verify",
        data=json.dumps({"otp": otp}, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
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
        raise OtpVerifyError(
            f"OpenBao отклонил SSH OTP: HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OtpVerifyError("OpenBao недоступен для проверки SSH OTP") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OtpVerifyError("OpenBao вернул некорректный ответ OTP") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise OtpVerifyError("OpenBao не подтвердил SSH OTP")

    returned_ip = data.get("ip")
    returned_user = data.get("username")
    if returned_user != username or returned_ip != expected_target:
        raise OtpVerifyError("SSH OTP выдан для другой цели или пользователя")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить SSH OTP через защищённый OpenBao"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"настройка проверки OTP (по умолчанию {DEFAULT_CONFIG})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    username = os.environ.get("PAM_USER", "")
    try:
        otp = read_otp(sys.stdin)
        verify_otp(args.config, otp, username=username)
    except OtpVerifyError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
