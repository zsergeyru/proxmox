#!/usr/bin/env python3
"""Показать сохранённый операторский пароль Authelia из доверенного PVE.

Не запускать в Semaphore и не передавать вывод в журналы.
Пароль не удаляется; при его смене показывается только предупреждение.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import yaml


PASSWORD = Path("/var/lib/web-gateway/secrets/operator-admin.json")
USERS = Path("/var/lib/web-gateway/authelia/users_database.yml")


def main() -> int:
    """Показать только действующий сохранённый пароль администратора."""
    if os.geteuid() != 0:
        raise PermissionError("Требуется root в госте 140")
    if not PASSWORD.is_file() or PASSWORD.is_symlink():
        print("Первоначальный пароль не сохранён. Нужен явный сброс пароля.")
        return 1
    if stat.S_IMODE(PASSWORD.stat().st_mode) & 0o077:
        raise PermissionError("Операторский файл имеет открытые права")
    credential = json.loads(PASSWORD.read_text(encoding="utf-8"))
    users = yaml.safe_load(USERS.read_text(encoding="utf-8"))
    admin = (users or {}).get("users", {}).get("admin", {})
    if (
        not isinstance(credential, dict)
        or credential.get("username") != "admin"
        or not isinstance(credential.get("password"), str)
        or not credential["password"]
        or not isinstance(admin, dict)
        or credential.get("password_hash") != admin.get("password")
    ):
        print("Первоначальный пароль изменён либо запись повреждена; старый пароль не показывается.")
        return 1
    print("Администратор: admin")
    print("Пароль:", credential["password"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"140: ошибка получения пароля: {error}", file=sys.stderr)
        sys.exit(1)
