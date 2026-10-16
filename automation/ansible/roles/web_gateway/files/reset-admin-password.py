#!/usr/bin/env python3
"""Явно задать новый пароль admin Authelia из доверенного терминала PVE.

Не выполняется Ansible и не запускается из Semaphore. Остальные пользователи,
секреты OIDC и зарегистрированный второй фактор не изменяются.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile

from argon2 import PasswordHasher
import yaml


STATE = Path("/var/lib/web-gateway")
USERS = STATE / "authelia/users_database.yml"
CREDENTIAL = STATE / "management/operator-admin.json"
LOCK = STATE / "secrets/.initialize.lock"


def replace_private(path: Path, value: str) -> None:
    """Атомарно заменить один закрытый файл без перехода по ссылкам."""
    if path.is_symlink():
        raise RuntimeError(f"Недопустимая ссылка: {path}")
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".admin-reset-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main() -> int:
    """Изменить только пароль существующего admin после проверки mp0."""
    if os.geteuid() != 0:
        raise PermissionError("Команду выполняет root")
    if not os.path.ismount(STATE):
        raise RuntimeError("Постоянное хранилище 140 не подключено")
    for path in (USERS, LOCK):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Нет защищённого файла: {path}")
    if stat.S_IMODE(USERS.stat().st_mode) & 0o077:
        raise RuntimeError("Файл пользователей имеет открытые права")
    if CREDENTIAL.parent.is_symlink() or not CREDENTIAL.parent.is_dir():
        raise RuntimeError("Нет постоянного каталога операторских данных")

    with LOCK.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        database = yaml.safe_load(USERS.read_text(encoding="utf-8"))
        admin = (database or {}).get("users", {}).get("admin")
        if not isinstance(admin, dict) or not isinstance(admin.get("password"), str):
            raise RuntimeError("Пользователь admin не найден. Восстановите файл пользователей.")
        password = secrets.token_urlsafe(24)
        fingerprint = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=2
        ).hash(password)
        admin["password"] = fingerprint
        # Сначала заменяется файл пользователей. Если запись пароля не удастся,
        # общий статус не покажет старый пароль как действующий.
        replace_private(
            USERS,
            yaml.safe_dump(database, allow_unicode=True, sort_keys=False),
        )
        replace_private(
            CREDENTIAL,
            json.dumps({
                "username": "admin",
                "password": password,
                "password_hash": fingerprint,
            }, ensure_ascii=False) + "\n",
        )
    print("Администратор: admin")
    print("Новый пароль:", password)
    print("Второй фактор и остальные пользователи не изменялись.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"140: пароль не изменён или сохранён не полностью: {error}", file=sys.stderr)
        sys.exit(1)
