#!/usr/bin/env python3
"""Проверки сетевой диагностики без реальных внешних запросов."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/infra-manager/jobs/check-runtime-downloads.py"
spec = importlib.util.spec_from_file_location("check_runtime_downloads", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

PACKER_URL = "https://releases.hashicorp.com/packer/1.15.4/packer_1.15.4_linux_amd64.zip"
PACKER_IMAGE = (
    "hashicorp/packer:1.15.4@"
    "sha256:8be72877859e00e13124b26373f0505b2a2597c5393ecbea900be28b4be58186"
)


def test_curl_uses_small_get_and_preserves_https() -> None:
    result = SimpleNamespace(
        returncode=0, stdout="HTTP/2 206\r\n\r\n__RUNTIME_DOWNLOAD_HTTP__:206\n"
    )
    with patch.object(module.subprocess, "run", return_value=result) as mocked:
        assert module.probe("Packer: архив", PACKER_URL) is None
    argv = mocked.call_args.args[0]
    assert "--range" in argv and argv[argv.index("--range") + 1] == "0-0"
    assert "--max-filesize" in argv
    assert argv[argv.index("--proto") + 1] == "=https"
    assert argv[argv.index("--proto-redir") + 1] == "=https"
    assert argv[-1] == PACKER_URL


def test_geo_blocking_is_not_mistaken_for_missing_file() -> None:
    headers = "HTTP/2 404\r\nx-amzn-waf-reason: geo\r\nserver: CloudFront\r\n"
    message = module.diagnose("Packer: архив", PACKER_URL, 0, 404, headers)
    assert message is not None
    assert "географическая блокировка" in message
    assert "Keenetic" in message
    assert "releases.hashicorp.com" in message
    assert "VPN" in message


def test_other_http_404_is_not_called_geo_block() -> None:
    message = module.diagnose("Packer: архив", PACKER_URL, 0, 404, "HTTP/2 404\n")
    assert message is not None and "HTTP 404" in message
    assert "географическая" not in message
    assert "версию" in message


def test_dns_tls_network_and_server_errors() -> None:
    samples = [
        (6, 0, "DNS", "Keenetic"),
        (60, 0, "TLS", "сертификаты"),
        (35, 0, "TLS", "HTTPS"),
        (28, 0, "время", "маршрут"),
        (56, 0, "прервано", "VPN"),
        (0, 403, "HTTP 403", "VPN"),
        (0, 429, "HTTP 429", "позднее"),
        (0, 503, "HTTP 503", "позднее"),
    ]
    for code, status, reason, advice in samples:
        message = module.diagnose("Packer", PACKER_URL, code, status, "")
        assert message is not None and reason in message, (code, status, message)
        assert advice in message, (code, status, message)
    assert module.diagnose("Packer", PACKER_URL, 0, 200, "") is None
    assert module.diagnose("Packer", PACKER_URL, 0, 206, "") is None
    # Если сервер игнорирует Range, curl ограничит скачивание кодом 63.
    assert module.diagnose("Packer", PACKER_URL, 63, 200, "") is None
    assert module.diagnose("Packer", PACKER_URL, 56, 200, "") is not None


def test_timeout_and_missing_curl_have_short_advice() -> None:
    with patch.object(
        module.subprocess, "run", side_effect=module.subprocess.TimeoutExpired(["curl"], 25)
    ):
        message = module.probe("Packer", PACKER_URL)
        assert message is not None and "время ожидания" in message
    with patch.object(module.subprocess, "run", side_effect=FileNotFoundError()):
        message = module.probe("Packer", PACKER_URL)
        assert message is not None and "curl" in message


def test_sources_come_from_versions_and_known_domains() -> None:
    urls = module.sources("1.12.6", "x86_64")
    assert len(urls) == 2
    assert urls[0][1].endswith("tofu_1.12.6_linux_amd64.zip")
    assert "SHA256SUMS" in urls[1][1]
    arm_urls = module.sources("1.12.6", "aarch64")
    assert arm_urls[0][1].endswith("linux_arm64.zip")
    module.validate_packer_image(PACKER_IMAGE, "1.15.4")
    for invalid in (
        "hashicorp/packer:1.15.3@" + PACKER_IMAGE.split("@")[1],
        "untrusted/packer:1.15.4@" + PACKER_IMAGE.split("@")[1],
        "hashicorp/packer:1.15.4",
        "hashicorp/packer:1.15.4@sha256:invalid",
        PACKER_IMAGE + "?token=secret",
    ):
        try:
            module.validate_packer_image(invalid, "1.15.4")
        except ValueError:
            continue
        raise AssertionError(f"Недостоверный образ Packer принят: {invalid}")


def test_docker_manifest_is_checked_without_pull() -> None:
    response = SimpleNamespace(returncode=0, stdout="{}", stderr="")
    with patch.object(module.subprocess, "run", return_value=response) as mocked:
        assert module.probe_packer_image(PACKER_IMAGE) is None
    assert mocked.call_args.args[0] == ["docker", "manifest", "inspect", PACKER_IMAGE]


def test_docker_manifest_reports_useful_failure() -> None:
    response = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="manifest unknown",
    )
    with patch.object(module.subprocess, "run", return_value=response):
        error = module.probe_packer_image(PACKER_IMAGE)
    assert error is not None
    assert "packer_image" in error
    assert "sha256" not in error


def test_failure_prints_only_reason_and_advice() -> None:
    response = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="Get https://registry-1.docker.io/v2/: i/o timeout",
    )
    out = io.StringIO()
    with (
        patch.object(module.platform, "machine", return_value="x86_64"),
        patch.object(module.subprocess, "run", return_value=response) as mocked,
        contextlib.redirect_stdout(out),
    ):
        code = module.main(
            [
                "--packer-version", "1.15.4",
                "--packer-image", PACKER_IMAGE,
                "--opentofu-version", "1.12.6",
            ]
        )
    assert code == 1
    assert mocked.call_count == 1
    assert len(out.getvalue().splitlines()) == 2
    assert "Keenetic" in out.getvalue()
    assert "Traceback" not in out.getvalue()


def test_success_checks_packer_and_opentofu() -> None:
    def run(args, **kwargs):
        del kwargs
        if args[:3] == ["docker", "manifest", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if args[0] == "curl":
            return SimpleNamespace(
                returncode=0,
                stdout="HTTP/2 206\\r\\n\\r\\n__RUNTIME_DOWNLOAD_HTTP__:206\\n",
            )
        raise AssertionError(f"Unexpected call: {args}")

    out = io.StringIO()
    with (
        patch.object(module.platform, "machine", return_value="x86_64"),
        patch.object(module.subprocess, "run", side_effect=run) as mocked,
        contextlib.redirect_stdout(out),
    ):
        code = module.main(
            [
                "--packer-version", "1.15.4",
                "--packer-image", PACKER_IMAGE,
                "--opentofu-version", "1.12.6",
            ]
        )
    assert code == 0
    assert mocked.call_count == 3
    assert "образ Packer" in out.getvalue() or "Образ Packer" in out.getvalue()


def main() -> None:
    tests = [
        test_curl_uses_small_get_and_preserves_https,
        test_geo_blocking_is_not_mistaken_for_missing_file,
        test_other_http_404_is_not_called_geo_block,
        test_dns_tls_network_and_server_errors,
        test_timeout_and_missing_curl_have_short_advice,
        test_sources_come_from_versions_and_known_domains,
        test_docker_manifest_is_checked_without_pull,
        test_docker_manifest_reports_useful_failure,
        test_failure_prints_only_reason_and_advice,
        test_success_checks_packer_and_opentofu,
    ]
    for test in tests:
        test()
    print(f"[ОК] Проверки сетевой диагностики пройдены: {len(tests)}")


if __name__ == "__main__":
    main()
