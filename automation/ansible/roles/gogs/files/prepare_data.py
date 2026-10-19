#!/usr/bin/env python3
"""Безопасная подготовка перенесённых данных Gogs 310.

Не создаёт базу, пользователей или репозитории. Вся работа выполняется
только на настоящем подключённом постоянном томе.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = ".gogs-imported"
MARKER_CONTENT = "gogs-data-v1\n"
GOGS_UID = 1000
GOGS_GID = 1000


class DataError(RuntimeError):
    """Данные не соответствуют проверенной исходной установке Gogs."""


def regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise DataError(f"Нет обычного файла: {path}")


def directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise DataError(f"Нет каталога: {path}")


def read_config(path: Path) -> configparser.ConfigParser:
    regular(path)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        # В app.ini Gogs допустимы RUN_MODE и RUN_USER до первой секции.
        # configparser без служебного заголовка такой INI отвергает.
        parser.read_string("[__global__]\n" + path.read_text(encoding="utf-8"))
    except (UnicodeError, configparser.Error, OSError) as exc:
        raise DataError("Не удалось прочитать постоянный app.ini") from exc
    if parser.get("database", "TYPE", fallback="").lower().strip() != "sqlite3":
        raise DataError("Исходная база должна быть SQLite")
    if parser.get("database", "PATH", fallback="").strip() != "data/gogs.db":
        raise DataError("Неожиданный путь SQLite: сначала проверить исходный Gogs")
    if parser.get("repository", "ROOT", fallback="").strip() != "/data/git/gogs-repositories":
        raise DataError("Неожиданный путь репозиториев: сначала проверить исходный Gogs")
    key = parser.get("security", "SECRET_KEY", fallback="").strip()
    if not key or key in {"CHANGE-ME-OR-FAIL-TO-START", "CHANGE_ME", "CHANGE-ME"}:
        raise DataError("Секрет Gogs отсутствует: восстановить исходный app.ini")
    return parser


def check_sqlite(path: Path) -> None:
    regular(path)
    if path.stat().st_size < 1024:
        raise DataError("Файл SQLite подозрительно мал")
    try:
        # mode=ro не создаёт новую базу при отсутствии файла.
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10) as conn:
            if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise DataError("SQLite не прошла проверку целостности")
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='user'"
            ).fetchone()
            if not row:
                raise DataError("В SQLite отсутствует таблица пользователей Gogs")
    except sqlite3.Error as exc:
        raise DataError("Не удалось прочитать существующую SQLite") from exc


def verify_data(data: Path, *, require_marker: bool = True, require_mount: bool = True) -> None:
    directory(data)
    if require_mount and not os.path.ismount(data):
        raise DataError(f"Отсутствует настоящий mount: {data}")
    directory(data / "gogs")
    directory(data / "gogs/conf")
    directory(data / "gogs/data")
    directory(data / "git")
    directory(data / "git/gogs-repositories")
    read_config(data / "gogs/conf/app.ini")
    check_sqlite(data / "gogs/data/gogs.db")
    if not any((data / "git/gogs-repositories").iterdir()):
        raise DataError("Репозитории отсутствуют: не запускать пустой Gogs")
    if require_marker:
        marker = data / MARKER
        regular(marker)
        if marker.read_text(encoding="utf-8") != MARKER_CONTENT:
            raise DataError("Не подтверждён перенос полного каталога Gogs")


def ensure_stopped() -> None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "gogs"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DataError("Docker должен быть установлен до подтверждения переноса") from exc
    if result.returncode == 0 and result.stdout.strip() == "true":
        raise DataError("Перед подтверждением переноса остановить контейнер gogs")


def change_owner(data: Path) -> None:
    # Не следовать символическим ссылкам в данных репозиториев.
    for base, directories, files in os.walk(data, followlinks=False):
        for name in [*directories, *files]:
            item = Path(base) / name
            os.chown(item, GOGS_UID, GOGS_GID, follow_symlinks=False)
    os.chown(data, GOGS_UID, GOGS_GID, follow_symlinks=False)


def confirm(data: Path) -> None:
    verify_data(data, require_marker=False)
    ensure_stopped()
    if (data / MARKER).exists():
        verify_data(data)
        print("GOGS_ALREADY_PREPARED")
        return
    change_owner(data)
    marker = data / MARKER
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(MARKER_CONTENT)
    marker.chmod(0o600)
    os.chown(marker, GOGS_UID, GOGS_GID)
    print("GOGS_IMPORT_CONFIRMED")


def rewrite_ini(content: str, changes: dict[str, dict[str, str]]) -> str:
    """Заменить только перечисленные настройки, сохранив остальные строки."""
    original = content.splitlines(keepends=True)
    result: list[str] = []
    section = ""
    seen_sections: set[str] = set()
    seen_keys: dict[str, set[str]] = {name: set() for name in changes}

    def append_missing(name: str) -> None:
        if name in changes:
            for key, value in changes[name].items():
                if key not in seen_keys[name]:
                    result.append(f"{key} = {value}\n")

    for line in original:
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            append_missing(section)
            section = header.group(1).strip().lower()
            if section in changes and section in seen_sections:
                raise DataError(f"Повторяющийся раздел app.ini: {section}")
            seen_sections.add(section)
            result.append(line)
            continue
        pair = re.match(r"^(\s*)([A-Za-z][A-Za-z0-9_]*)(\s*=\s*)(.*)$", line)
        if pair and section in changes:
            key = pair.group(2).upper()
            if key in changes[section]:
                if key in seen_keys[section]:
                    raise DataError(f"Повторяющийся параметр {section}.{key}")
                seen_keys[section].add(key)
                result.append(f"{pair.group(1)}{pair.group(2)}{pair.group(3)}{changes[section][key]}\n")
                continue
        result.append(line)
    append_missing(section)
    for name, keys in changes.items():
        if name not in seen_sections:
            result.extend([f"\n[{name}]\n", *(f"{key} = {value}\n" for key, value in keys.items())])
    return "".join(result)


def configure(data: Path, domain: str, url: str, ssh_port: int) -> None:
    verify_data(data)
    if domain != "git.zsergey.ru" or url != "https://git.zsergey.ru/" or ssh_port != 2222:
        raise DataError("Недопустимые параметры публикации Gogs 310")
    path = data / "gogs/conf/app.ini"
    changes = {
        "server": {
            "DOMAIN": domain,
            "EXTERNAL_URL": url,
            "PROTOCOL": "http",
            "HTTP_PORT": "3000",
            "DISABLE_SSH": "false",
            "START_SSH_SERVER": "true",
            "SSH_PORT": str(ssh_port),
            "SSH_DOMAIN": "192.168.3.10",
            "SSH_LISTEN_PORT": str(ssh_port),
        },
        "auth": {
            "REQUIRE_SIGNIN_VIEW": "true",
            "DISABLE_REGISTRATION": "true",
            "ENABLE_REVERSE_PROXY_AUTHENTICATION": "false",
        },
        "repository": {"FORCE_PRIVATE": "true", "DISABLE_HTTP_GIT": "false"},
        "security": {"COOKIE_SECURE": "true"},
        "session": {"COOKIE_SECURE": "true"},
    }
    old = path.read_text(encoding="utf-8")
    new = rewrite_ini(old, changes)
    if old == new:
        print("GOGS_CONFIG_UNCHANGED")
        return
    backup = path.with_name("app.ini.before-310")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        os.chown(backup, GOGS_UID, GOGS_GID)
    fd, temporary = tempfile.mkstemp(prefix=".app.ini.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.chown(temporary, GOGS_UID, GOGS_GID)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("GOGS_CONFIG_CHANGED")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--confirm", action="store_true")
    actions.add_argument("--configure", action="store_true")
    parser.add_argument("--domain", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--ssh-port", type=int, default=0)
    args = parser.parse_args()
    data = args.data
    if not data.is_absolute():
        raise DataError("Постоянный каталог должен иметь абсолютный путь")
    if args.confirm:
        confirm(data)
    elif args.configure:
        configure(data, args.domain, args.url, args.ssh_port)
    else:
        verify_data(data)
        print("GOGS_DATA_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DataError, OSError, UnicodeError) as exc:
        print(f"[ОШИБКА] Gogs 310: {exc}", file=sys.stderr)
        raise SystemExit(1)
