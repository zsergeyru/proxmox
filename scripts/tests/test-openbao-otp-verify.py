#!/usr/bin/env python3
"""Проверки целевого helper OpenBao SSH OTP."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = (
    ROOT
    / "automation/ansible/roles/linux_base/files/openbao-otp-verify.py"
)


def fail(message: str) -> None:
    raise SystemExit(message)


def load_helper():
    spec = importlib.util.spec_from_file_location(
        "openbao_otp_verify_test",
        HELPER_PATH,
    )
    if spec is None or spec.loader is None:
        fail("Не удалось загрузить helper проверки OpenBao OTP")
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


def write_config(root: Path, *, address: str = "https://192.168.9.10:8202") -> Path:
    ca = root / "ca.crt"
    ca.write_text("TEST CA\n", encoding="utf-8")
    config = root / "otp-verify.env"
    config.write_text(
        "\n".join(
            (
                f"OPENBAO_ADDR={address}",
                f"OPENBAO_CA={ca}",
                "OPENBAO_SSH_MOUNT=ssh-otp",
                "OPENBAO_TARGET_IP=192.168.3.11",
                "",
            )
        ),
        encoding="utf-8",
    )
    return config


def test_verify_otp() -> None:
    helper = load_helper()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = write_config(root)
        context = SimpleNamespace(check_hostname=False, verify_mode=None)
        calls: list[dict[str, object]] = []

        def fake_urlopen(request, *, context, timeout):
            calls.append(
                {
                    "url": request.full_url,
                    "body": json.loads(request.data.decode("utf-8")),
                    "headers": {
                        key.lower(): value
                        for key, value in request.header_items()
                    },
                    "context": context,
                    "timeout": timeout,
                }
            )
            return FakeResponse(
                {
                    "data": {
                        "ip": "192.168.3.11",
                        "username": "root",
                    }
                }
            )

        with (
            patch.object(
                helper.ssl,
                "create_default_context",
                return_value=context,
            ) as create_context,
            patch.object(
                helper.urllib.request,
                "urlopen",
                side_effect=fake_urlopen,
            ),
        ):
            helper.verify_otp(config, "OTP-SECRET", username="root")

        create_context.assert_called_once()
        if context.check_hostname is not True:
            fail("Helper не включил проверку имени TLS")
        if context.verify_mode != helper.ssl.CERT_REQUIRED:
            fail("Helper не требует проверку TLS-сертификата")
        if len(calls) != 1:
            fail("Helper должен выполнить ровно один запрос проверки OTP")
        call = calls[0]
        if call["url"] != "https://192.168.9.10:8202/v1/ssh-otp/verify":
            fail(f"Helper использовал неверный endpoint: {call!r}")
        if call["body"] != {"otp": "OTP-SECRET"}:
            fail("Helper передал OpenBao неверный OTP payload")
        if "x-vault-token" in call["headers"]:
            fail("Проверка OTP не должна использовать OpenBao token")


def test_rejects_wrong_target_or_user() -> None:
    helper = load_helper()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = write_config(root)
        context = SimpleNamespace(check_hostname=False, verify_mode=None)

        def fake_wrong_target(request, *, context, timeout):
            del request, context, timeout
            return FakeResponse(
                {"data": {"ip": "192.168.4.10", "username": "root"}}
            )

        with (
            patch.object(helper.ssl, "create_default_context", return_value=context),
            patch.object(
                helper.urllib.request,
                "urlopen",
                side_effect=fake_wrong_target,
            ),
        ):
            try:
                helper.verify_otp(config, "OTP-SECRET", username="root")
            except helper.OtpVerifyError:
                pass
            else:
                fail("Helper принял OTP, выданный для другой цели")

        try:
            helper.verify_otp(config, "OTP-SECRET", username="admin")
        except helper.OtpVerifyError:
            pass
        else:
            fail("Helper разрешил OTP не для root")


def test_rejects_insecure_address() -> None:
    helper = load_helper()
    with tempfile.TemporaryDirectory() as temporary:
        config = write_config(
            Path(temporary),
            address="http://192.168.9.10:8202",
        )
        try:
            helper.verify_otp(config, "OTP-SECRET", username="root")
        except helper.OtpVerifyError as exc:
            if "HTTPS" not in str(exc):
                fail(f"Неожиданная ошибка небезопасного адреса: {exc}")
        else:
            fail("Helper принял HTTP вместо HTTPS")


def test_http_error_does_not_echo_otp() -> None:
    helper = load_helper()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = write_config(root)
        context = SimpleNamespace(check_hostname=False, verify_mode=None)

        def deny(request, *, context, timeout):
            del request, context, timeout
            raise urllib.error.HTTPError(
                "https://192.168.9.10:8202/v1/ssh-otp/verify",
                400,
                "bad request",
                {},
                None,
            )

        with (
            patch.object(helper.ssl, "create_default_context", return_value=context),
            patch.object(helper.urllib.request, "urlopen", side_effect=deny),
        ):
            try:
                helper.verify_otp(config, "OTP-SECRET", username="root")
            except helper.OtpVerifyError as exc:
                if "OTP-SECRET" in str(exc):
                    fail("Helper раскрыл OTP в тексте ошибки")
            else:
                fail("Helper скрыл отказ OpenBao")


def main() -> None:
    test_verify_otp()
    test_rejects_wrong_target_or_user()
    test_rejects_insecure_address()
    test_http_error_does_not_echo_otp()
    print("OpenBao OTP verifier tests passed.")


if __name__ == "__main__":
    main()
