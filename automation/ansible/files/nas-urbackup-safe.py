#!/usr/bin/env python3
"""Безопасная проверка и первичная инициализация UrBackup физического NAS.

Никогда не форматирует, не создаёт пулы и не трогает чужие архивы.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/etc/nas-apps/urbackup/config.json")
COMPOSE = Path("/etc/nas-apps/urbackup/compose.yml")
ROOT = Path("/var/lib/nas-apps/urbackup")
PASSWORD = Path("/etc/nas-apps/urbackup/admin-password")
INITIALIZED = Path("/etc/nas-apps/urbackup/initialized")
ALLOWED = ("nas-data", "nas-backup")


class SafetyError(Exception):
    pass


def call(*args: str) -> str:
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       check=False, timeout=35)
    if p.returncode:
        raise SafetyError("Проверка NAS завершилась ошибкой: " + args[0])
    return p.stdout.strip()


def read_config() -> dict:
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyError("Отсутствует проверенное описание UrBackup NAS") from exc
    if set(raw) != {"source_pool", "source_guid", "source_mount",
                    "backup_pool", "backup_guid", "backup_mount"}:
        raise SafetyError("Неверное описание хранилищ UrBackup")
    if raw["source_pool"] != "nas-data" or raw["backup_pool"] != "nas-backup":
        raise SafetyError("Нельзя менять назначение пулов NAS")
    if raw["source_guid"] == raw["backup_guid"]:
        raise SafetyError("Источник и резерв должны быть разными физическими пулами")
    for name in ("source_guid", "backup_guid"):
        if not re.fullmatch(r"[0-9]{1,20}", str(raw[name])):
            raise SafetyError("Неизвестный GUID пула")
    for name in ("source_mount", "backup_mount"):
        mount = raw[name]
        if (not isinstance(mount, str) or
                not re.fullmatch(r"/[A-Za-z0-9_./-]+", mount) or
                "/../" in mount or mount == "/" or
                os.path.realpath(mount) != mount):
            raise SafetyError("Небезопасная точка подключения NAS")
    if raw["source_mount"] == raw["backup_mount"]:
        raise SafetyError("Два пула используют одинаковую точку")
    return raw


def verify() -> dict:
    c = read_config()
    for role in ("source", "backup"):
        pool = c[role + "_pool"]
        mount = c[role + "_mount"]
        if call("zpool", "get", "-H", "-o", "value", "guid", pool) != c[role + "_guid"]:
            raise SafetyError("Подменён GUID пула " + pool)
        if call("zpool", "list", "-H", "-o", "health", pool) != "ONLINE":
            raise SafetyError("Пул не в состоянии ONLINE: " + pool)
        source = call("findmnt", "-n", "-M", mount, "-o", "SOURCE")
        filesystem = call("findmnt", "-n", "-M", mount, "-o", "FSTYPE")
        if filesystem != "zfs" or source.split("/", 1)[0] != pool:
            raise SafetyError("Резервный том не подключён: " + pool)
        if not Path(mount).is_dir():
            raise SafetyError("Не существует точка подключения: " + mount)
    backup_root = Path(c["backup_mount"]) / "urbackup"
    if backup_root.exists() and backup_root.is_symlink():
        raise SafetyError("Символьная ссылка вместо каталога UrBackup запрещена")
    return c


def _safe_dir(parent: Path, child: str, *, mode: int = 0o750) -> Path:
    path = parent / child
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise SafetyError("Опасная замена каталога UrBackup")
    path.mkdir(mode=mode, exist_ok=True)
    return path


def prepare() -> None:
    c = verify()
    location = Path(c["backup_mount"])
    base = _safe_dir(location, "urbackup")
    _safe_dir(base, "backups")
    recovery = _safe_dir(base, "recovery", mode=0o700)
    os.chmod(recovery, 0o700)
    app = _safe_dir(Path("/var/lib/nas-apps"), "urbackup")
    _safe_dir(app, "server")
    # Меняется только собственный каталог, никогда весь /var или nas-backup.
    if not (COMPOSE.is_file() and CONFIG.is_file()):
        raise SafetyError("Не установлен проверенный Compose или config.json")


def initialize_admin() -> None:
    verify()
    if INITIALIZED.exists():
        if not PASSWORD.exists():
            raise SafetyError("Отсутствует исходный защищённый пароль администратора")
        return
    db_dir = ROOT / "server"
    if any(db_dir.iterdir()):
        raise SafetyError("Есть база без отметки инициализации; не перезаписываем её")
    # Секрет не попадает в Git, аргументы Ansible, командный вывод или журналы.
    pwd = secrets.token_urlsafe(36)
    fd = os.open(str(PASSWORD), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(pwd + "\n")
    args = ["docker", "compose", "-f", str(COMPOSE), "run", "--rm",
            "--no-deps", "-T", "urbackup", "reset-admin-pw",
            "-a", "admin", "-p", pwd]
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=180, check=False)
        if result.returncode != 0:
            raise SafetyError("Не удалось безопасно задать пароль администратора")
        fd = os.open(str(INITIALIZED), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as marker:
            marker.write("administrator-created\n")
    except (OSError, subprocess.TimeoutExpired):
        raise SafetyError("Автоматическая инициализация UrBackup не завершилась")


def main() -> int:
    try:
        action = sys.argv[1] if len(sys.argv) == 2 else ""
        if action == "preflight":
            verify()
            print("Подтверждены два физических ZFS-пула NAS")
        elif action == "prepare":
            prepare()
            print("Каталоги UrBackup готовы на подтверждённом резервном диске")
        elif action == "init-admin":
            initialize_admin()
            print("Защищённый администратор UrBackup создан или подтверждён")
        elif action == "status":
            verify()
            if not INITIALIZED.exists():
                raise SafetyError("UrBackup не инициализирован")
            print("NAS: диски и закрытая инициализация UrBackup проверены")
        else:
            raise SafetyError("Неподдерживаемое действие")
        return 0
    except SafetyError as exc:
        print("ОШИБКА: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
