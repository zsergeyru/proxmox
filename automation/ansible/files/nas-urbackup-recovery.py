#!/usr/bin/env python3
"""Обычный tar-архив состояния UrBackup: без шифрования и ключей.

Архивы создаются только на проверенном nas-backup, не публикуются по SMB
и доступны root. SHA-256 выявляет случайное повреждение, но не подмену
злоумышленником с доступом на запись.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

CONFIG = Path("/etc/nas-apps/urbackup/config.json")
STORE = Path("/var/lib/nas-apps/urbackup/server")
SERVICE = "nas-urbackup.service"
NAS_BACKUP = "nas-urbackup-local.service"
BACKUP_ROOT = Path("/etc/nas-apps/urbackup")
PATHS = (
    "var/lib/nas-apps/urbackup/server",
    "etc/nas-apps/urbackup/config.json",
    "etc/nas-apps/urbackup/compose.yml",
    "etc/nas-apps/urbackup/admin-password",
    "etc/nas-apps/urbackup/initialized",
)


class RecoveryError(Exception):
    pass


def cmd(*argv: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("Недоступна системная проверка: " + argv[0]) from exc
    if result.returncode:
        raise RecoveryError("Ошибка системной операции: " + argv[0])
    return result.stdout.strip()


def is_active(service: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError("Невозможно проверить службу: " + service) from exc


def destination() -> Path:
    cmd("/usr/local/sbin/nas-urbackup-safe", "preflight")
    try:
        configuration = json.loads(CONFIG.read_text(encoding="utf-8"))
        mount = Path(configuration["backup_mount"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecoveryError("Нет проверенной точки резервного диска") from exc
    root = mount / "urbackup"
    folder = root / "recovery"
    # Не следовать неожиданным ссылкам и не создавать mount автоматически.
    for part in (mount, root, folder):
        if part.is_symlink() or not part.is_dir():
            raise RecoveryError("Путь архивов не подтверждён: " + str(part))
    if folder.resolve() != folder or not folder.resolve().is_relative_to(mount.resolve()):
        raise RecoveryError("Путь копии указывает за пределы резервного тома")
    os.chmod(folder, 0o700)
    return folder


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def check_archive(archive: Path, expected: str) -> None:
    if archive.is_symlink() or not archive.is_file():
        raise RecoveryError("Недостоверный файл резервной копии")
    if not archive.name.startswith("state-") or archive.suffix != ".tar":
        raise RecoveryError("Неверное имя архива состояния")
    if digest(archive) != expected:
        raise RecoveryError("Контрольная сумма архива не совпадает")
    try:
        with tarfile.open(archive, "r:") as package:
            names = package.getnames()
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise RecoveryError("Архив состояния невозможно прочитать") from exc
    if not any(name == PATHS[0] or name.startswith(PATHS[0] + "/") for name in names):
        raise RecoveryError("В архиве отсутствует состояние UrBackup Server")
    for item in PATHS[1:]:
        if item not in names:
            raise RecoveryError("В архиве не хватает служебного файла UrBackup")


def verify() -> None:
    folder = destination()
    archives = sorted(folder.glob("state-*.tar"), reverse=True)
    if not archives:
        raise RecoveryError("Нет сохранённых комплектов состояния UrBackup")
    archive = archives[0]
    checksum = archive.with_suffix(".sha256")
    if checksum.is_symlink() or not checksum.is_file():
        raise RecoveryError("Отсутствует контрольная сумма состояния UrBackup")
    try:
        expected = checksum.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RecoveryError("Невозможно прочитать контрольную сумму") from exc
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise RecoveryError("Неверная контрольная сумма состояния UrBackup")
    check_archive(archive, expected)
    print("Последний архив состояния UrBackup читается, SHA-256 совпадает")


def backup() -> None:
    folder = destination()
    if is_active(NAS_BACKUP):
        raise RecoveryError("Идёт локальное резервирование: базу сервера не останавливать")
    if not is_active(SERVICE):
        raise RecoveryError("UrBackup Server не запущен")
    if not STORE.is_dir() or STORE.is_symlink():
        raise RecoveryError("Не найдена постоянная база UrBackup")
    if any(not (Path("/") / p).exists() for p in PATHS):
        raise RecoveryError("Не все обязательные файлы состояния UrBackup существуют")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = folder / ("state-" + stamp + ".tar")
    partial = folder / ("." + archive.name + ".incomplete")
    if partial.exists() or archive.exists():
        raise RecoveryError("Архив этого запуска уже существует")

    stop_attempted = False
    failed = None
    try:
        stop_attempted = True
        cmd("systemctl", "stop", SERVICE)
        fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            try:
                result = subprocess.run(
                    ["tar", "-C", "/", "--numeric-owner", "-cf", "-", *PATHS],
                    stdout=stream, stderr=subprocess.DEVNULL,
                    timeout=1800, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RecoveryError("Не удалось создать обычный tar-архив") from exc
            stream.flush()
            os.fsync(stream.fileno())
        if result.returncode or partial.stat().st_size == 0:
            raise RecoveryError("Архивация состояния UrBackup не завершена")
        checksum = digest(partial)
        # Проверить структуру до объявления новой копии готовой.
        temporary_named = folder / ("state-" + stamp + ".tar")
        partial.replace(temporary_named)
        archive = temporary_named
        check_archive(archive, checksum)
        sum_path = archive.with_suffix(".sha256")
        fd = os.open(str(sum_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(checksum + "\n")
            out.flush()
            os.fsync(out.fileno())
    except Exception as exc:
        failed = exc
    finally:
        if stop_attempted:
            try:
                cmd("systemctl", "start", SERVICE, timeout=180)
                if not is_active(SERVICE):
                    raise RecoveryError("UrBackup не запустился после копирования состояния")
            except RecoveryError as exc:
                failed = exc if failed is None else RecoveryError(
                    str(failed) + "; также не удалось запустить UrBackup"
                )
    if failed is not None:
        raise failed
    print("Обычный tar-архив состояния UrBackup сохранён и проверен (без шифрования)")


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("backup", "verify"):
            raise RecoveryError("Доступны только backup и verify")
        if sys.argv[1] == "backup":
            backup()
        else:
            verify()
        return 0
    except (OSError, ValueError, RecoveryError) as exc:
        print("ОШИБКА UrBackup recovery: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
