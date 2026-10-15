#!/usr/bin/env python3
"""Создать отсутствующие секреты 140 только на постоянном томе.

Программа выполняется во время развёртывания, не запускает службу и не
показывает пароль в журналах. Существующие значения никогда не меняет.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile

from argon2 import PasswordHasher
from passlib.hash import pbkdf2_sha512
import yaml


def write_once(path: Path, value: str) -> bool:
    """Атомарно создать защищённый файл, никогда не заменяя существующий."""
    if path.is_symlink():
        raise RuntimeError(f"Обнаружена символическая ссылка: {path}")
    if path.exists():
        check_file(path)
        return False
    fd, name = tempfile.mkstemp(prefix=".initial-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(name, path)
            created = True
        except FileExistsError:
            check_file(path)
            created = False
        return created
    finally:
        os.unlink(name)


def check_file(path: Path) -> None:
    """Отклонить отсутствующий, пустой, подменённый или открытый файл."""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Отсутствует или повреждён защищённый файл: {path}")
    st = path.stat()
    if not st.st_size or stat.S_IMODE(st.st_mode) & 0o077:
        raise RuntimeError(f"Неверные права или пустой защищённый файл: {path}")


def rsa_private_key() -> str:
    """Получить закрытый RSA-ключ OIDC без передачи значения в аргументах."""
    result = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA",
         "-pkeyopt", "rsa_keygen_bits:3072"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout


def initialize(state: Path, email: str) -> None:
    """Инициализировать приложение один раз и проверять целостность повторно."""
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise RuntimeError("Неверный адрес администратора в provision.yaml")
    users_dir = state / "authelia"
    keys_dir = state / "secrets"
    if not state.is_dir() or not users_dir.is_dir() or not keys_dir.is_dir():
        raise RuntimeError("140: отсутствуют каталоги на проверенном mp0")
    if keys_dir.is_symlink() or users_dir.is_symlink():
        raise RuntimeError("140: подмена каталога состояния символической ссылкой")
    os.chmod(keys_dir, 0o700)
    marker = keys_dir / ".initialized"
    users = users_dir / "users_database.yml"
    required = (
        "authelia-session-secret",
        "authelia-storage-encryption-key",
        "authelia-jwt-secret",
        "authelia-oidc-hmac-secret",
        "authelia-oidc-jwks-pem",
        "authelia-homarr-client-secret-hash",
        "authelia-immich-client-secret-hash",
        "homarr.env",
        "immich-client-secret",
    )
    with (keys_dir / ".initialize.lock").open("a+") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            check_file(marker)
            check_file(users)
            for name in required:
                check_file(keys_dir / name)
            with users.open(encoding="utf-8") as file:
                database = yaml.safe_load(file)
            if "admin" not in (database or {}).get("users", {}):
                raise RuntimeError("140: администратор отсутствует; восстановить пользователей")
            return
        # Не считать утраченную метку или частично созданные ключи новой установкой.
        if (users.exists() or (users_dir / "db.sqlite3").exists()
                or any((state / "homarr").iterdir())
                or any((keys_dir / name).exists() for name in required)):
            raise RuntimeError("140: найдены прежние данные без метки инициализации; восстановить копию")

        homarr_client = secrets.token_hex(32)
        immich_client = secrets.token_hex(32)
        values = {
            "authelia-session-secret": secrets.token_urlsafe(48),
            "authelia-storage-encryption-key": secrets.token_urlsafe(48),
            "authelia-jwt-secret": secrets.token_urlsafe(48),
            "authelia-oidc-hmac-secret": secrets.token_urlsafe(48),
            "authelia-oidc-jwks-pem": rsa_private_key(),
            "authelia-homarr-client-secret-hash": pbkdf2_sha512.using(rounds=310000).hash(homarr_client),
            "authelia-immich-client-secret-hash": pbkdf2_sha512.using(rounds=310000).hash(immich_client),
            "homarr.env": (
                f"SECRET_ENCRYPTION_KEY={secrets.token_hex(32)}\n"
                f"AUTH_OIDC_CLIENT_SECRET={homarr_client}\n"
            ),
            "immich-client-secret": immich_client,
        }
        for name, value in values.items():
            write_once(keys_dir / name, value + ("" if value.endswith("\n") else "\n"))

        password = secrets.token_urlsafe(24)
        hash_value = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(password)
        data = {"users": {"admin": {
            "displayname": "Администратор",
            "password": hash_value,
            "email": email,
            "groups": ["admins"],
        }}}
        write_once(users, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

        temporary = Path("/run/web-gateway")
        temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(temporary, 0o700)
        write_once(temporary / "initial-admin-password", password + "\n")
        write_once(marker, "v1\n")


def main() -> int:
    """Запустить подготовку с явными путями без секретов в аргументах."""
    if os.geteuid() != 0 or len(sys.argv) != 3:
        raise RuntimeError("Запускать только root с каталогом и адресом администратора")
    initialize(Path(sys.argv[1]), sys.argv[2])
    print("140: постоянные пользователи и секреты проверены (без вывода значений)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"140: ошибка подготовки: {error}", file=sys.stderr)
        sys.exit(1)
