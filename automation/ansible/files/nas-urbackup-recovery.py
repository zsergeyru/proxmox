#!/usr/bin/env python3
"""Шифрованный согласованный комплект состояния UrBackup (без очистки чужих данных).

Для полной аварийной восстановления закрытый ключ ДОЛЖЕН быть сохранён вне NAS.
Обычная работа копирования от отсутствия внешнего ключа не блокируется.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

CONFIG = Path("/etc/nas-apps/urbackup/config.json")
KEY = Path("/etc/nas-apps/urbackup/recovery-key")
STORE = Path("/var/lib/nas-apps/urbackup/server")
SERVICE = "nas-urbackup.service"
NAS_BACKUP = "nas-urbackup-local.service"


class RecoveryError(Exception):
    pass


def cmd(*argv: str) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("Системная проверка состояния недоступна") from exc
    if proc.returncode:
        raise RecoveryError("Системная проверка состояния не пройдена: " + argv[0])
    return proc.stdout.strip()


def destination() -> Path:
    cmd("/usr/local/sbin/nas-urbackup-safe", "preflight")
    c = json.loads(CONFIG.read_text(encoding="utf-8"))
    d = Path(c["backup_mount"]) / "urbackup" / "recovery"
    if not d.is_dir() or d.is_symlink() or not d.resolve().is_relative_to(
        Path(c["backup_mount"]).resolve()
    ):
        raise RecoveryError("Недоступен защищённый каталог recovery на NAS")
    return d


def ensure_key() -> None:
    d = destination()
    if KEY.exists():
        if not KEY.is_file() or KEY.is_symlink() or KEY.stat().st_mode & 0o077:
            raise RecoveryError("Небезопасные права ключа восстановления")
        return
    if list(d.glob("state-*.enc")):
        raise RecoveryError("Ключ отсутствует при существующих архивах; новый создавать нельзя")
    key = secrets.token_urlsafe(48)
    fd = os.open(str(KEY), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(key + "\n")


def backup() -> None:
    d = destination()
    ensure_key()
    if not STORE.is_dir():
        raise RecoveryError("Нет постоянной базы UrBackup")
    if subprocess.run(["systemctl", "is-active", "--quiet", NAS_BACKUP],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      timeout=20, check=False).returncode == 0:
        raise RecoveryError("Идёт локальное файловое копирование; база не останавливается")
    key = KEY.read_text(encoding="utf-8").strip()
    if len(key) < 48:
        raise RecoveryError("Ключ восстановления слишком короткий")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = d / ("state-" + stamp + ".enc")
    temp = d / ("." + output.name + ".incomplete")
    if output.exists() or temp.exists():
        raise RecoveryError("Комплект состояния уже существует")
    # Вся база, ключ сервера и настройка администратора одной временной точки.
    paths = [
        "var/lib/nas-apps/urbackup/server",
        "etc/nas-apps/urbackup/admin-password",
        "etc/nas-apps/urbackup/initialized",
    ]
    if not all((Path("/") / p).exists() for p in paths):
        raise RecoveryError("Неполный исходный комплект состояния")
    running = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0
    if not running:
        raise RecoveryError("UrBackup Server не работает; не останавливать и не упаковывать непроверенную базу")
    try:
        cmd("systemctl", "stop", SERVICE)
        env = os.environ.copy()
        env["NAS_URBACKUP_RECOVERY_KEY"] = key
        with temp.open("xb") as encrypted:
            tar = subprocess.Popen(["tar", "-C", "/", "-cf", "-", *paths],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
            try:
                enc = subprocess.run(["openssl", "enc", "-aes-256-cbc",
                                      "-salt", "-pbkdf2", "-iter", "300000",
                                      "-pass", "env:NAS_URBACKUP_RECOVERY_KEY"],
                                     stdin=tar.stdout, stdout=encrypted,
                                     stderr=subprocess.DEVNULL, env=env,
                                     check=False, timeout=1800)
            finally:
                assert tar.stdout is not None
                tar.stdout.close()
            tar_rc = tar.wait(timeout=30)
        if tar_rc or enc.returncode or temp.stat().st_size < 32:
            raise RecoveryError("Шифрование комплекта состояния не завершилось")
        # CBC не даёт встроенной аутентификации: HMAC защищает от подмены.
        mac = hmac.new(key.encode("utf-8"), digestmod=hashlib.sha256)
        with temp.open("rb") as data:
            for block in iter(lambda: data.read(1024 * 1024), b""):
                mac.update(block)
        os.chmod(temp, 0o600)
        temp.replace(output)
        tag = output.with_suffix(".hmac")
        fd = os.open(str(tag), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out_mac:
            out_mac.write(mac.hexdigest() + "\n")
        print("Согласованный зашифрованный комплект UrBackup сохранён")
    finally:
        # При любой ошибке вернуть службу, не удалять последний исправный архив.
        subprocess.run(["systemctl", "start", SERVICE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=180, check=False)
        # Недописанный комплект — диагностическое свидетельство сбоя.
        # Он не объявляется успешным и не удаляется слепо.


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise RecoveryError("Ожидается команда key или backup")
        if sys.argv[1] == "key":
            ensure_key()
            print("Ключ состояния создан или подтверждён; нужен независимый экспорт")
        elif sys.argv[1] == "backup":
            backup()
        else:
            raise RecoveryError("Неизвестное действие")
        return 0
    except (OSError, ValueError, RecoveryError) as exc:
        print("ОШИБКА UrBackup recovery: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
