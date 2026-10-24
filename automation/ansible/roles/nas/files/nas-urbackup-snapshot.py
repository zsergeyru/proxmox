#!/usr/bin/env python3
"""Одно согласованное файловое резервирование NAS через UrBackup Client.

Перед записью подтверждает оба ZFS-пула, никогда не форматирует диски.
При отсутствии завершённой копии сообщает об ошибке; снимок держит до конца.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path("/etc/nas-apps/urbackup/config.json")
STAGING = Path("/run/nas-urbackup-source")
MOUNT = STAGING / "current"
GUEST_SOURCE = "/backup-source/current"
MOUNT_GUARD = "/usr/local/sbin/nas-urbackup-client-mount"
LOCK = Path("/run/lock/nas-urbackup-snapshot.lock")
CLIENT = ("docker", "exec", "nas-urbackup-client", "urbackupclientctl")
PREFLIGHT = "/usr/local/sbin/nas-urbackup-safe"


class SnapshotError(Exception):
    pass


def run(*argv: str, timeout: int = 60) -> str:
    try:
        p = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("Не завершилась операция " + argv[0]) from exc
    if p.returncode != 0:
        raise SnapshotError("Ошибка операции " + argv[0])
    return p.stdout.strip()


def client_status() -> dict:
    """Get client connection status, not its restore-only browse API."""
    raw = run(*CLIENT, "status")
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise SnapshotError("UrBackup Client не вернул корректный статус JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("servers"), list):
        raise SnapshotError("Неизвестный формат статуса UrBackup Client")
    return obj


def server_current(config: dict) -> str | None:
    """Read server's *completed* file-backup pointer (never its temp files).

    Per upstream, the server updates current only after all files are
    downloaded. A successful client-side transfer alone is not enough.
    """
    root = Path(config["backup_mount"]) / "urbackup" / "backups"
    if root.is_symlink() or not root.is_dir():
        raise SnapshotError("Недостоверен каталог резервного тома UrBackup")
    client = root / "nas-local"
    if not client.exists():
        return None
    if client.is_symlink() or not client.is_dir():
        raise SnapshotError("Недостоверен каталог клиента nas-local")
    current = client / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise SnapshotError("Нет подтверждённого current-снимка UrBackup Server")
    try:
        target = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError("Не читается ссылка current в хранилище UrBackup") from exc
    # Refuse a redirect into an unrelated directory or another ZFS pool.
    if not target.is_relative_to(client.resolve()) or target == client:
        raise SnapshotError("Ссылка UrBackup current выходит за каталог клиента")
    if not target.is_dir():
        raise SnapshotError("Неверный каталог последней завершённой копии")
    # File backup sets are stored under the logical backupdir name.
    # This prevents counting a completed but empty/unrelated backup.
    if not (target / "nas-data").is_dir():
        return None
    return str(target)


def main() -> int:
    if os.geteuid() != 0:
        raise SnapshotError("Нужны привилегии для ZFS и bind-mount")
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(PREFLIGHT, "preflight")
        run(MOUNT_GUARD, "check")
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        src = config["source_mount"]
        ds = config["source_pool"]
        # Дочерние datasets требуют отдельных снимков и mount: не пропускаем их.
        children = run("zfs", "list", "-H", "-o", "name", "-r", ds).splitlines()
        if children != [ds]:
            raise SnapshotError("Обнаружены дочерние ZFS datasets: нужна согласованная схема")
        if MOUNT.is_symlink() or (MOUNT.exists() and
                                  (not MOUNT.is_dir() or os.path.ismount(str(MOUNT))
                                   or any(MOUNT.iterdir()))):
            raise SnapshotError("Временная точка ZFS уже занята — проверить прошлое задание")
        old_snapshots = run("zfs", "list", "-H", "-t", "snapshot",
                            "-o", "name", "-r", ds)
        if any(name.startswith(ds + "@urbackup-") for name in old_snapshots.splitlines()):
            raise SnapshotError("Сохранился снимок незавершённого задания; нельзя создавать новый")
        connection = client_status()
        if not connection["servers"] or connection.get("internet_status") != "connected_local":
            raise SnapshotError("UrBackup Client ещё не подключён к локальному серверу")
        previous = server_current(config)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = "urbackup-" + stamp + "-" + str(os.getpid())
        if not re.fullmatch(r"urbackup-[0-9T]+Z-[0-9]+", name):
            raise SnapshotError("Неверное имя временного снимка")
        snapshot = ds + "@" + name
        mounted = False
        created = False
        configured = False
        confirmed = False
        try:
            run("zfs", "snapshot", snapshot)
            created = True
            source_snapshot = Path(src) / ".zfs" / "snapshot" / name
            if not source_snapshot.is_dir():
                raise SnapshotError("ZFS snapshot недоступен для чтения")
            MOUNT.mkdir(mode=0o700, exist_ok=True)
            run("mount", "--bind", str(source_snapshot), str(MOUNT))
            mounted = True
            run("mount", "-o", "remount,bind,ro", str(MOUNT))
            if not os.path.ismount(str(MOUNT)):
                raise SnapshotError("Нельзя подтвердить bind-mount согласованного снимка")
            # Verify Docker actually received the *mounted* ZFS snapshot.
            # Directory existence alone would allow a misleading empty backup.
            mountinfo = run("docker", "exec", "nas-urbackup-client",
                            "cat", "/proc/self/mountinfo")
            if not any(
                len(fields) > 5 and fields[4] == GUEST_SOURCE
                and "ro" in fields[5].split(",")
                for fields in (line.split() for line in mountinfo.splitlines())
            ):
                raise SnapshotError(
                    "Docker не видит read-only mount ZFS снимка: резервирование запрещено"
                )
            # Директория клиента существует ТОЛЬКО во время жизни снимка.
            # В остальное время нет пути, из которого UrBackup мог бы
            # автоматически сохранить пустой каталог вместо файлов NAS.
            run(*CLIENT, "add-backupdir", "-n", "nas-data", "-d", GUEST_SOURCE)
            configured = True
            if GUEST_SOURCE not in run(*CLIENT, "list-backupdirs"):
                raise SnapshotError("Клиент не подтвердил включение пути ZFS snapshot")
            # Upstream start -f *blocks* until the exact file-backup process
            # reports success. Server-side file organization may continue.
            run(*CLIENT, "start", "-f", timeout=21600)
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                current = server_current(config)
                if current is not None and current != previous:
                    confirmed = True
                    print("UrBackup NAS: клиент завершил задание, сервер опубликовал новую полную копию")
                    return 0
                time.sleep(5)
            raise SnapshotError("Сервер не подтвердил новый завершённый архив nas-local/current")
        finally:
            if not confirmed:
                # Неподтверждённое задание может всё ещё читать файлы.
                # Не отключать источник и не уничтожать снимок до
                # диагностики; следующая операция явно обнаружит занятый mount.
                print("ПРЕДУПРЕЖДЕНИЕ: временный ZFS snapshot сохранён "
                      "до проверки завершения клиента", file=sys.stderr)
                if created:
                    return 2
            if configured:
                run(*CLIENT, "remove-backupdir", "-n", "nas-data")
            if mounted:
                # Никогда не удалять снимок, пока его читает резервная задача.
                # При проблеме unmount (busy) оставляем снимок и сообщаем об ошибке.
                try:
                    run("umount", str(MOUNT), timeout=90)
                    mounted = False
                except SnapshotError:
                    raise SnapshotError("Снимок занят клиентом; отказ от его удаления")
            if not mounted and MOUNT.exists():
                MOUNT.rmdir()
            if created and not mounted:
                run("zfs", "destroy", snapshot)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SnapshotError, BlockingIOError, OSError, ValueError) as exc:
        print("ОШИБКА UrBackup NAS: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
