#!/usr/bin/env python3
"""Управление учётными записями Authelia на постоянном томе гостя 140.

Все изменения явные; повторный deploy не управляет пользовательскими данными.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile

from argon2 import PasswordHasher
import yaml

STATE = Path("/var/lib/web-gateway")
USERS = STATE / "authelia/users_database.yml"
LOCK = STATE / "secrets/.initialize.lock"
GROUPS = frozenset({"admins", "family", "guests"})
NAME = re.compile(r"[a-z][a-z0-9_.-]{1,31}\Z")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


def validate_name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise ValueError("Имя: от 2 до 32 латинских букв, цифр, точки, дефиса или подчёркивания")
    return value


def validate_groups(raw: str) -> list[str]:
    groups = [value.strip() for value in raw.split(",")]
    if not groups or any(group not in GROUPS for group in groups) or len(set(groups)) != len(groups):
        raise ValueError("Разрешены только группы admins, family, guests без повторений")
    return groups


def require_state(state: Path) -> None:
    if not os.path.ismount(state):
        raise RuntimeError("Постоянный том 140 не подключён")
    users = state / "authelia/users_database.yml"
    lock = state / "secrets/.initialize.lock"
    for path in (users, lock):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Постоянные пользовательские данные или замок отсутствуют")
    if stat.S_IMODE(users.stat().st_mode) & 0o077:
        raise RuntimeError("Доступ к файлу пользователей слишком открыт")


def read_users(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("users"), dict):
        raise RuntimeError("Повреждён файл пользователей")
    return value


def replace_users(path: Path, document: dict) -> None:
    """Сохранить изменения атомарно, без ссылок и с закрытыми правами."""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Файл пользователей изменился или исчез")
    content = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".users-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def active_admins(users: dict) -> int:
    return sum(
        isinstance(record, dict) and "admins" in record.get("groups", [])
        and record.get("disabled") is not True
        for record in users.values()
    )


def change(state: Path, action: str, username: str, *,
           email: str = "", displayname: str = "", groups: str = "",
           revoke_totp: Callable[[str], None] | None = None) -> str | None:
    """Применить одну операцию под общим замком и проверить последнего администратора."""
    validate_name(username)
    require_state(state)
    path = state / "authelia/users_database.yml"
    with (state / "secrets/.initialize.lock").open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        document = read_users(path)
        users = document["users"]
        record = users.get(username)
        password: str | None = None
        if action == "add":
            if record is not None:
                raise ValueError("Учётная запись уже существует")
            # Authelia требует непустое поле email, но фактическая почта
            # не нужна: проверочные коды выдаёт администратор из уведомителя.
            # Служебный адрес уникален и не используется для доставки писем.
            email = email.strip() or f"{username}@users.zsergey.ru"
            if not EMAIL.fullmatch(email):
                raise ValueError("Неверный адрес пользователя")
            if any(isinstance(v, dict) and v.get("email", "").lower() == email.lower()
                   for v in users.values()):
                raise ValueError("Указанный адрес уже принадлежит другому пользователю")
            selected = validate_groups(groups)
            password = secrets.token_urlsafe(24)
            users[username] = {
                "displayname": displayname.strip() or username,
                "password": PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(password),
                "email": email,
                "groups": selected,
                "disabled": False,
            }
        elif action in {"password", "groups", "edit", "disable", "enable", "delete"}:
            if not isinstance(record, dict):
                raise ValueError("Учётная запись не найдена")
            if action == "password":
                if username == "admin":
                    raise ValueError("Для admin используйте штатный reset-admin-password.py")
                password = secrets.token_urlsafe(24)
                record["password"] = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(password)
            elif action in {"groups", "edit"}:
                record["groups"] = validate_groups(groups)
                if action == "edit":
                    name = displayname.strip()
                    if not 1 <= len(name) <= 60:
                        raise ValueError("Отображаемое имя должно содержать от 1 до 60 символов")
                    record["displayname"] = name
            elif action == "delete":
                if username == "admin":
                    raise ValueError("Первоначальную учётную запись admin удалить нельзя")
                if revoke_totp is None:
                    raise RuntimeError("Удаление требует очистки второго фактора")
                # Сначала удалить регистрацию TOTP. При ошибке учётная
                # запись остаётся действующей, чтобы не оставить старый
                # второй фактор у нового владельца того же имени.
                revoke_totp(username)
                del users[username]
            else:
                record["disabled"] = action == "disable"
        else:
            raise ValueError("Неизвестная операция")
        if active_admins(users) < 1:
            raise ValueError("Нельзя отключить или лишить прав последнего администратора")
        replace_users(path, document)
        return password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="Показать пользователей и группы (без паролей)")
    add = sub.add_parser("add", help="Создать учётную запись и показать её пароль")
    add.add_argument("username")
    add.add_argument("--email", required=True)
    add.add_argument("--name", default="")
    add.add_argument("--groups", required=True)
    for name in ("password", "disable", "enable"):
        sub.add_parser(name).add_argument("username")
    groups = sub.add_parser("groups")
    groups.add_argument("username")
    groups.add_argument("--groups", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("доступно только администратору гостя 140")
    try:
        require_state(STATE)
        if args.action == "list":
            records = read_users(USERS)["users"]
            for username, record in sorted(records.items()):
                print(f"{username}: {','.join(record.get('groups', []))} "
                      f"({'отключён' if record.get('disabled') else 'доступен'})")
        else:
            password = change(
                STATE, args.action, args.username,
                email=getattr(args, "email", ""),
                displayname=getattr(args, "name", ""),
                groups=getattr(args, "groups", ""),
            )
            print(f"140: {args.username}: действие {args.action} выполнено")
            if password is not None:
                print("Новый пароль:", password)
                print("Не сохраняйте его в журналы или Git.")
        return 0
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        print(f"140: изменения не применены: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
