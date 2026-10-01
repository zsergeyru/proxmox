#!/usr/bin/env python3
"""Проверки гостевого клиента OpenBao SSH OTP."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = (
    ROOT
    / "automation/ansible/roles/linux_base/files/openbao-otp-client.py"
)


def fail(message: str) -> None:
    raise SystemExit(message)


def load_client():
    spec = importlib.util.spec_from_file_location(
        "openbao_otp_client_test",
        CLIENT_PATH,
    )
    if spec is None or spec.loader is None:
        fail("Не удалось загрузить клиент OpenBao OTP")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, exc, traceback
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, separators=(",", ":")).encode("utf-8")


def test_request_otp() -> None:
    client = load_client()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ca = root / "ca.crt"
        ca.write_text("TEST CA\n", encoding="utf-8")
        config = root / "machine.env"
        config.write_text(
            "\n".join(
                (
                    "OPENBAO_ADDR=https://192.168.9.10:8202",
                    "OPENBAO_ROLE_ID=ROLE-ID-SECRET",
                    "OPENBAO_SECRET_ID=SECRET-ID-SECRET",
                    f"OPENBAO_CA={ca}",
                    "OPENBAO_SSH_OTP_ROLE=guest-410",
                    "",
                )
            ),
            encoding="utf-8",
        )

        responses = iter(
            (
                {"auth": {"client_token": "MACHINE-TOKEN-SECRET"}},
                {
                    "data": {
                        "key": "OTP-SECRET",
                        "key_type": "otp",
                        "ip": "192.168.3.11",
                        "port": 22,
                        "username": "root",
                    }
                },
            )
        )
        calls: list[dict[str, object]] = []
        context = SimpleNamespace(check_hostname=False, verify_mode=None)

        def fake_urlopen(request, *, context, timeout):
            body = json.loads(request.data.decode("utf-8"))
            headers = {key.lower(): value for key, value in request.header_items()}
            calls.append(
                {
                    "url": request.full_url,
                    "body": body,
                    "token": headers.get("x-vault-token"),
                    "context": context,
                    "timeout": timeout,
                }
            )
            return FakeResponse(next(responses))

        with (
            patch.object(
                client.ssl,
                "create_default_context",
                return_value=context,
            ) as create_context,
            patch.object(client.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            otp = client.request_otp(config, "192.168.3.11")

        if otp != "OTP-SECRET":
            fail("OTP-клиент вернул неожиданное значение")
        create_context.assert_called_once_with(cafile=str(ca))
        if context.check_hostname is not True:
            fail("OTP-клиент не включил проверку имени TLS")
        if context.verify_mode != client.ssl.CERT_REQUIRED:
            fail("OTP-клиент не требует проверку TLS-сертификата")

        if [call["url"] for call in calls] != [
            "https://192.168.9.10:8202/v1/auth/machine/login",
            "https://192.168.9.10:8202/v1/ssh-otp/creds/guest-410",
        ]:
            fail(f"Неожиданная последовательность OpenBao API: {calls!r}")
        if calls[0]["token"] is not None:
            fail("AppRole login не должен использовать существующий token")
        if calls[1]["token"] != "MACHINE-TOKEN-SECRET":
            fail("OTP-запрос не использовал машинный token")
        if calls[1]["body"] != {
            "ip": "192.168.3.11",
            "username": "root",
        }:
            fail("OTP-клиент передал неверную цель")


def test_rejects_insecure_openbao_address() -> None:
    client = load_client()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ca = root / "ca.crt"
        ca.write_text("TEST CA\n", encoding="utf-8")
        config = root / "machine.env"
        config.write_text(
            "\n".join(
                (
                    "OPENBAO_ADDR=http://192.168.9.10:8202",
                    "OPENBAO_ROLE_ID=ROLE",
                    "OPENBAO_SECRET_ID=SECRET",
                    f"OPENBAO_CA={ca}",
                    "OPENBAO_SSH_OTP_ROLE=guest-410",
                    "",
                )
            ),
            encoding="utf-8",
        )
        try:
            client.request_otp(config, "192.168.3.11")
        except client.OtpClientError as exc:
            if "HTTPS" not in str(exc):
                fail(f"Неожиданная ошибка небезопасного адреса: {exc}")
        else:
            fail("OTP-клиент принял HTTP вместо HTTPS")


def test_token_revoked_on_invalid_credential() -> None:
    client = load_client()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ca = root / "ca.crt"
        ca.write_text("TEST CA\n", encoding="utf-8")
        config = root / "machine.env"
        config.write_text(
            "\n".join(
                (
                    "OPENBAO_ADDR=https://192.168.9.10:8202",
                    "OPENBAO_ROLE_ID=ROLE",
                    "OPENBAO_SECRET_ID=SECRET",
                    f"OPENBAO_CA={ca}",
                    "OPENBAO_SSH_OTP_ROLE=guest-410",
                    "",
                )
            ),
            encoding="utf-8",
        )
        responses = iter(
            (
                {"auth": {"client_token": "TOKEN"}},
                {"data": {"key": "BAD", "key_type": "otp"}},
                {},
            )
        )
        urls: list[str] = []
        context = SimpleNamespace(check_hostname=False, verify_mode=None)

        def fake_urlopen(request, *, context, timeout):
            del context, timeout
            urls.append(request.full_url)
            return FakeResponse(next(responses))

        with (
            patch.object(client.ssl, "create_default_context", return_value=context),
            patch.object(client.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            try:
                client.request_otp(config, "192.168.3.11")
            except client.OtpClientError:
                pass
            else:
                fail("OTP-клиент принял неполный credential")

        if urls[-1] != "https://192.168.9.10:8202/v1/auth/token/revoke-self":
            fail("Token не отозван после ошибки проверки OTP")


def test_rejects_invalid_target_and_user() -> None:
    client = load_client()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ca = root / "ca.crt"
        ca.write_text("TEST CA\n", encoding="utf-8")
        config = root / "machine.env"
        config.write_text(
            "\n".join(
                (
                    "OPENBAO_ADDR=https://192.168.9.10:8202",
                    "OPENBAO_ROLE_ID=ROLE",
                    "OPENBAO_SECRET_ID=SECRET",
                    f"OPENBAO_CA={ca}",
                    "OPENBAO_SSH_OTP_ROLE=guest-410",
                    "",
                )
            ),
            encoding="utf-8",
        )

        for target in ("example.org", "2001:db8::1"):
            try:
                client.request_otp(config, target)
            except client.OtpClientError:
                pass
            else:
                fail(f"OTP-клиент принял недопустимую цель: {target}")

        try:
            client.request_otp(config, "192.168.9.10", username="admin")
        except client.OtpClientError:
            pass
        else:
            fail("OTP-клиент разрешил SSH-пользователя не root")


def main() -> None:
    test_request_otp()
    test_rejects_insecure_openbao_address()
    test_token_revoked_on_invalid_credential()
    test_rejects_invalid_target_and_user()
    print("OpenBao OTP client tests passed.")


if __name__ == "__main__":
    main()
