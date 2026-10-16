#!/usr/bin/env python3
"""Первичная привязка TOTP администратора к Google Authenticator.

Вызывает только штатную CLI Authelia: не пишет SQLite и не раскрывает
секрет в журнал. Не применяет --force, чтобы не сбросить установленный TOTP.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def require(condition: bool, message: str) -> None:
    """Остановить подготовку при неожиданном состоянии."""
    if not condition:
        raise RuntimeError(message)


def initialize(container: str, username: str, issuer: str, png: Path, container_png: str) -> str:
    """Создать TOTP лишь при отсутствии защищённого QR на постоянном томе."""
    require(re.fullmatch(r"[A-Za-z0-9_-]+", container) is not None, "Неверное имя контейнера")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+", username) is not None, "Неверное имя пользователя")
    require(re.fullmatch(r"[A-Za-z0-9.-]+", issuer) is not None, "Неверный издатель TOTP")
    require(png.name == f"{username}.png", "Неожиданное имя файла QR")
    require(png.parent.is_dir() and not png.parent.is_symlink(), "Нет постоянного каталога QR")
    require(container_png == f"/data/totp-setup/{username}.png", "Неожиданный путь QR в контейнере")
    require(not png.is_symlink(), "Файл QR не может быть символической ссылкой")
    if png.is_file():
        # Действующий QR уже есть; не менять шифрованный ключ в базе.
        os.chmod(png, 0o600)
        return "QR первого администратора уже существует"
    require(not png.exists() and not png.is_symlink(), "Опасная ссылка или неверный файл QR")

    argv = [
        "docker", "exec", container, "authelia",
        "storage", "user", "totp", "generate", username,
        "--config", "/config/configuration.yml",
        "--issuer", issuer,
        "--path", container_png,
    ]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        # Уже зарегистрированный TOTP нельзя сбрасывать; для повторного
        # подключения потребуется явная процедура восстановления.
        description = (result.stderr + b"\n" + result.stdout).decode("utf-8", "replace").lower()
        if "already exists" in description or "already has" in description:
            return "У администратора уже есть TOTP; ключ не изменён"
        raise RuntimeError("Authelia не смогла создать TOTP; проверьте контейнер и SQLite")
    require(png.is_file() and not png.is_symlink(), "Authelia не создала QR на постоянном томе")
    os.chmod(png, 0o600)
    return "Google Authenticator: QR подготовлен для администратора"


def main() -> int:
    """Выполнить подготовку, не печатая ни QR, ни секрет из CLI."""
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
