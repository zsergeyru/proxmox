#!/usr/bin/env python3
"""Однократно подготовить TOTP администратора штатной CLI Authelia.

QR и URI остаются закрытыми на mp0 до подтверждения шестизначного кода.
Повторный запуск никогда не перевыпускает существующий TOTP.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse


def require(condition: bool, message: str) -> None:
    """Прекратить работу, не изменяя действующие ключи."""
    if not condition:
        raise RuntimeError(message)


def find_uri(data: bytes, username: str) -> str:
    """Выбрать URI конкретного пользователя из закрытого ответа CLI."""
    content = data.decode("utf-8", "replace")
    for match in re.findall(r"otpauth://totp/[^\s'\"<>]+", content):
        parsed = urlparse(match)
        label = unquote(parsed.path.lstrip("/"))
        if label.split(":")[-1] == username and "secret=" in parsed.query:
            return match
    raise RuntimeError("Authelia не вернула URI TOTP администратора")


def write_uri(path: Path, content: str) -> None:
    """Создать временную защищённую копию секрета без перезаписи."""
    require(not path.exists() and not path.is_symlink(), "URI уже существует либо подменён")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content + "\n")
        file.flush()
        os.fsync(file.fileno())


def initialize(container: str, username: str, issuer: str, png: Path, container_png: str) -> str:
    """Создать QR один раз либо подготовить URI для QR старой версии."""
    require(re.fullmatch(r"[A-Za-z0-9_-]+", container) is not None, "Неверное имя контейнера")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", username) is not None, "Неверное имя пользователя")
    require(re.fullmatch(r"[A-Za-z0-9.-]+", issuer) is not None, "Неверный издатель TOTP")
    require(png.name == f"{username}.png", "Неожиданное имя файла QR")
    require(png.parent.is_dir() and not png.parent.is_symlink(), "Нет постоянного каталога QR")
    require(container_png == f"/data/totp-setup/{username}.png", "Неожиданный путь QR в контейнере")
    uri_file = png.with_suffix(".uri")
    completed = png.parent / f"{username}.confirmed"
    require(not png.is_symlink() and not uri_file.is_symlink() and not completed.is_symlink(),
            "Файл первоначальной регистрации не может быть ссылкой")
    if completed.is_file():
        png.unlink(missing_ok=True)
        uri_file.unlink(missing_ok=True)
        return "Google Authenticator уже подтверждён; QR отсутствует"
    argv = ["docker", "exec", container, "authelia", "storage", "user", "totp"]
    if png.is_file():
        os.chmod(png, 0o600)
        if uri_file.is_file():
            os.chmod(uri_file, 0o600)
            return "QR первого администратора сохранён; ключ не изменён"
        # Ранее QR создавался без временного URI: экспортируем лишь для проверки кода.
        result = subprocess.run(argv + ["export", "uri", "--config", "/config/configuration.yml"],
                                capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError("Не удалось получить URI существующего TOTP")
        write_uri(uri_file, find_uri(result.stdout, username))
        return "QR сохранён; подтверждение первоначальной регистрации подготовлено"
    require(not png.exists(), "Неверный файл QR")
    # Никаких --force: уже зарегистрированное устройство остаётся прежним.
    result = subprocess.run(argv + [
        "generate", username, "--config", "/config/configuration.yml",
        "--issuer", issuer, "--path", container_png,
    ], capture_output=True, check=False)
    if result.returncode:
        output = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace").lower()
        if "already exists" in output or "already has" in output:
            return "TOTP уже существует, но QR удалён; автоматический сброс запрещён"
        raise RuntimeError("Authelia не смогла создать TOTP; проверьте контейнер и SQLite")
    require(png.is_file() and not png.is_symlink(), "Authelia не создала QR на постоянном томе")
    os.chmod(png, 0o600)
    write_uri(uri_file, find_uri(result.stdout, username))
    return "Google Authenticator: первоначальная регистрация подготовлена"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--png", type=Path, required=True)
    parser.add_argument("--container-png", required=True)
    args = parser.parse_args()
    try:
        print(initialize(args.container, args.username, args.issuer, args.png, args.container_png))
        return 0
    except RuntimeError as exc:
        print(f"140: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
