#!/usr/bin/env python3
"""Получение кода Authelia и явный сброс TOTP из доверенного терминала PVE.

В обычном состоянии не меняет файл пользователей и не печатает секреты.
Код выводится только при непосредственном запросе администратора.
"""
from __future__ import annotations

import argparse
from email.utils import parseaddr
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

STATE = Path("/var/lib/web-gateway/authelia")
USERS = STATE / "users_database.yml"
NOTIFICATIONS = STATE / "notifications.txt"
CONTAINER = "web-gateway-authelia"


def account(username: str, users: Path = USERS) -> dict:
    """Проверить существование указанного пользователя в основной базе."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+", username) is None:
        raise RuntimeError("Недопустимое имя пользователя")
    value = yaml.safe_load(users.read_text(encoding="utf-8"))
    record = (value or {}).get("users", {}).get(username)
    if not isinstance(record, dict) or not record.get("email"):
        raise RuntimeError("Пользователь отсутствует или у него не указан адрес")
    return record


def latest_code(username: str, users: Path = USERS, notifications: Path = NOTIFICATIONS) -> str:
    """Извлечь код только из последнего сообщения, адресованного пользователю."""
    record = account(username, users)
    if not notifications.is_file() or notifications.is_symlink():
        raise RuntimeError("Проверочный код ещё не запрошен")
    # Файловый уведомитель Authelia перезаписывает файл при новом сообщении.
    if notifications.stat().st_size > 32768:
        raise RuntimeError("Неожиданный размер файла уведомления")
    lines = notifications.read_text(encoding="utf-8").splitlines()
    headers = {}
    for line in lines[:6]:
        if line.startswith(("Date: ", "Recipient: ", "Subject: ")):
            key, value = line.split(": ", 1)
            headers[key] = value
    recipient = parseaddr(headers.get("Recipient", ""))[1]
    if recipient.lower() != record["email"].lower():
        raise RuntimeError("Последнее уведомление предназначено другому пользователю")
    if "confirm your identity" not in headers.get("Subject", "").lower():
        raise RuntimeError("Последнее уведомление не является подтверждением личности")
    body = "\n".join(lines[3:])
    codes = re.findall(r"\b[0-9]{8}\b", body)
    if len(codes) != 1:
        raise RuntimeError("Не удалось однозначно определить проверочный код")
    return codes[0]


def reset_totp(username: str, users: Path = USERS) -> None:
    """Удалить только регистрацию второго фактора средствами Authelia."""
    account(username, users)
    argv = ["docker", "exec", CONTAINER, "authelia", "storage", "user", "totp",
            "delete", username, "--config", "/config/configuration.yml"]
    result = subprocess.run(argv, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Authelia не смогла сбросить TOTP; база не изменена этим сценарием")
    # Успешный сброс не меняет пароль, группы, пользователей или другие базы.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    code = sub.add_parser("code", help="Показать последний проверочный код для пользователя")
    code.add_argument("username")
    reset = sub.add_parser("reset", help="Явно сбросить Google Authenticator пользователя")
    reset.add_argument("username")
    reset.add_argument("--confirm", action="store_true", help="Подтвердить сброс второго фактора")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("требуется root внутри гостя 140")
    try:
        if args.action == "code":
            print("Проверочный код " + args.username + ": " + latest_code(args.username))
        elif not args.confirm:
            parser.error("для сброса требуется --confirm")
        else:
            reset_totp(args.username)
            print("TOTP пользователя " + args.username + " сброшен; пароль и группы сохранены")
        return 0
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print("140: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
