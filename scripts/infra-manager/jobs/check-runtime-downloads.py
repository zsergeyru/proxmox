#!/usr/bin/env python3
"""Короткая проверка сетевых источников перед сборкой управляющей среды."""

from __future__ import annotations

import argparse
import platform
import re
import subprocess
from urllib.parse import urlsplit


HTTP_MARKER = "__RUNTIME_DOWNLOAD_HTTP__:"
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
GEO_RE = re.compile(r"^x-amzn-waf-reason:\s*geo(?:\s|$)", re.IGNORECASE | re.MULTILINE)
PACKER_IMAGE_RE = re.compile(r"hashicorp/packer:(?P<version>[0-9]+(?:\.[0-9]+){2})@sha256:[0-9a-f]{64}")


def diagnose(name: str, url: str, code: int, status: int, headers: str) -> str | None:
    """Вернуть причину и совет без вывода технических данных и секретов."""
    if status in (200, 206) and code in (0, 63):
        # Код 63 допустим только при HTTP 200: сервер не поддержал Range,
        # и curl прервал пробу по ограничению размера скачивания.
        return None

    host = urlsplit(url).hostname or "сервер"
    if GEO_RE.search(headers):
        cause = f"HTTP {status}: географическая блокировка со стороны CloudFront"
        advice = (
            f"Направьте {host} через доступный VPN в правилах Keenetic; "
            "проверьте внешний адрес VPN и повторите операцию."
        )
    elif status == 404:
        cause = "HTTP 404: ресурс по указанному адресу недоступен"
        advice = (
            "Проверьте версию и адрес файла; если ресурс существует, "
            "проверьте маршрут к серверу и ограничения CDN."
        )
    elif status in (401, 403):
        cause = f"HTTP {status}: сервер ограничил доступ"
        advice = "Проверьте доступность из другой сети и правила VPN/прокси."
    elif status == 429:
        cause = "HTTP 429: превышен лимит запросов"
        advice = "Повторите операцию позднее; проверьте ограничения внешнего адреса."
    elif status >= 500:
        cause = f"HTTP {status}: ошибка удалённого сервера"
        advice = "Проверьте состояние поставщика и повторите операцию позднее."
    elif code == 6:
        cause = f"DNS: имя {host} не разрешается"
        advice = "Проверьте DNS гостя, Keenetic и правила выбора DNS."
    elif code == 60:
        cause = "TLS: не удалось проверить сертификат сервера"
        advice = "Проверьте системное время, доверенные корневые сертификаты и HTTPS-прокси."
    elif code == 35:
        cause = "TLS: соединение не удалось согласовать"
        advice = "Проверьте HTTPS-перехват, фильтрацию, сетевой маршрут и VPN."
    elif code == 28:
        cause = "Превышено время ожидания соединения или ответа"
        advice = "Проверьте доступ к серверу, маршрут и VPN."
    elif code in (7, 52, 55, 56):
        cause = "Соединение с сервером не установлено или прервано"
        advice = "Проверьте фильтрацию, соединение с провайдером и маршрут через VPN."
    elif code == 127:
        cause = "Отсутствует команда curl"
        advice = "Установите curl через штатную настройку ОС гостя."
    else:
        cause = f"Не удалось получить ресурс (HTTP {status or 'нет'}, код curl {code})"
        advice = "Проверьте подключение, настройки прокси и доступность адреса."

    return f"[ОШИБКА] {name}: {cause}.\n[РЕШЕНИЕ] {advice}"


def probe(name: str, url: str) -> str | None:
    """GET с Range и пределом размера, без скачивания большого архива."""
    argv = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--proto", "=https",
        "--proto-redir", "=https",
        "--connect-timeout", "8",
        "--max-time", "20",
        "--max-filesize", "1048576",
        "--range", "0-0",
        "--dump-header", "-",
        "--output", "/dev/null",
        "--write-out", f"\\n{HTTP_MARKER}%{{http_code}}\\n",
        url,
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=25, check=False
        )
    except subprocess.TimeoutExpired:
        return diagnose(name, url, 28, 0, "")
    except FileNotFoundError:
        return diagnose(name, url, 127, 0, "")

    matches = re.findall(re.escape(HTTP_MARKER) + r"(\d{3})", result.stdout)
    status = int(matches[-1]) if matches else 0
    return diagnose(name, url, result.returncode, status, result.stdout)


def validate_packer_image(image: str, packer_version: str) -> None:
    """Допускать только официальный образ с указанной версией и SHA256."""
    match = PACKER_IMAGE_RE.fullmatch(image)
    if not match or match.group("version") != packer_version:
        raise ValueError(
            "Packer: версия или контрольная сумма официального Docker-образа "
            "не согласована с provision.yaml"
        )


def probe_packer_image(image: str) -> str | None:
    """Проверить манифест через Docker Hub, не скачивая все слои образа."""
    try:
        result = subprocess.run(
            ["docker", "manifest", "inspect", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            "[ОШИБКА] Packer: время ожидания Docker Hub истекло.\n"
            "[РЕШЕНИЕ] Проверьте доступ к Docker Hub и маршрут через Keenetic/VPN."
        )
    except FileNotFoundError:
        return (
            "[ОШИБКА] Packer: команда Docker недоступна.\n"
            "[РЕШЕНИЕ] Проверьте установку Docker на управляющем госте."
        )

    if result.returncode == 0:
        return None
    details = result.stderr.lower()
    if "no such manifest" in details or "manifest unknown" in details or "not found" in details:
        cause = "официальный образ с выбранной версией и SHA256 не найден"
        advice = "Проверьте значение packer_image в provision.yaml и его контрольную сумму."
    elif "lookup " in details or "no such host" in details:
        cause = "DNS не разрешает адрес Docker Hub"
        advice = "Проверьте DNS управляющего гостя и Keenetic."
    elif "x509" in details or "certificate" in details:
        cause = "ошибка проверки сертификата при доступе к Docker Hub"
        advice = "Проверьте время, доверенные сертификаты и настройки прокси."
    elif "timeout" in details or "deadline exceeded" in details:
        cause = "истекло время ожидания Docker Hub"
        advice = "Проверьте сетевой маршрут и правила VPN на Keenetic."
    elif "unauthorized" in details or "denied" in details or "429" in details:
        cause = "Docker Hub отказал в доступе или ограничил запросы"
        advice = "Проверьте доступ к Docker Hub и ограничения внешнего IP."
    else:
        cause = "не удалось получить манифест официального образа"
        advice = "Проверьте доступ к Docker Hub и правила маршрутизации через Keenetic/VPN."
    return f"[ОШИБКА] Packer: {cause}.\n[РЕШЕНИЕ] {advice}"


def sources(opentofu_version: str, machine: str) -> list[tuple[str, str]]:
    if not VERSION_RE.fullmatch(opentofu_version):
        raise ValueError("Некорректная версия OpenTofu в provision.yaml")
    architectures = {"x86_64": "amd64", "aarch64": "arm64"}
    if machine not in architectures:
        raise ValueError(f"Неподдерживаемая архитектура: {machine}")
    arch = architectures[machine]
    tofu = f"https://github.com/opentofu/opentofu/releases/download/v{opentofu_version}"
    return [
        ("OpenTofu: архив", f"{tofu}/tofu_{opentofu_version}_linux_{arch}.zip"),
        ("OpenTofu: SHA256", f"{tofu}/tofu_{opentofu_version}_SHA256SUMS"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить внешние источники для infra-runtime.")
    parser.add_argument("--packer-version", required=True)
    parser.add_argument("--packer-image", required=True)
    parser.add_argument("--opentofu-version", required=True)
    args = parser.parse_args(argv)

    try:
        validate_packer_image(args.packer_image, args.packer_version)
        checks = sources(args.opentofu_version, platform.machine())
    except ValueError as exc:
        print(f"[ОШИБКА] {exc}")
        return 1

    error = probe_packer_image(args.packer_image)
    if error:
        print(error)
        return 1

    for name, url in checks:
        error = probe(name, url)
        if error:
            print(error)
            return 1
    print("[ОК] Образ Packer и архивы OpenTofu доступны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
