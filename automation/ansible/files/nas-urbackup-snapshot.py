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
LOCK = Path("/run/lock/nas-urbackup-snapshot.lock")
CLIENT = "/usr/local/bin/urbackupclientctl"
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


def list_backups() -> set[int]:
    # Не считаем зелёный docker/systemd доказательством резервной копии.
    raw = run(CLIENT, "browse")
    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise SnapshotError("Нет достоверной истории UrBackup Client") from exc
    if not isinstance(items, list):
        raise SnapshotError("Некорректная история файловых копий")
    return {int(x["id"]) for x in items if isinstance(x, dict) and
            isinstance(x.get("id"), int)}


def main() -> int:
    if os.geteuid() != 0:
        raise SnapshotError("Нужны привилегии для ZFS и bind-mount")
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(PREFLIGHT, "preflight")
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        src = config["source_mount"]
        ds = "nas-data"
        # Дочерние datasets требуют отдельных снимков и mount: не пропускаем их.
        children = run("zfs", "list", "-H", "-o", "name", "-r", ds).splitlines()
        if children != [ds]:
            raise SnapshotError("Обнаружены дочерние ZFS datasets: нужна согласованная схема")
        previous = list_backups()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = "urbackup-" + stamp + "-" + str(os.getpid())
        if not re.fullmatch(r"urbackup-[0-9T]+Z-[0-9]+", name):
            raise SnapshotError("Неверное имя временного снимка")
        snapshot = ds + "@" + name
        mounted = False
        created = False
        configured = False
        try:
            run("zfs", "snapshot", snapshot)
            created = True
            source_snapshot = Path(src) / ".zfs" / "snapshot" / name
            if not source_snapshot.is_dir():
                raise SnapshotError("ZFS snapshot недоступен для чтения")
            if STAGING.is_symlink() or (STAGING.exists() and
                                       os.path.ismount(str(STAGING))):
                raise SnapshotError("Точка UrBackup уже занята")
            STAGING.mkdir(mode=0o700, exist_ok=True)
            run("mount", "--bind", str(source_snapshot), str(STAGING))
            mounted = True
            run("mount", "-o", "remount,bind,ro", str(STAGING))
            if not os.path.ismount(str(STAGING)):
                raise SnapshotError("Нельзя подтвердить bind-mount согласованного снимка")
            # Директория клиента существует ТОЛЬКО во время жизни снимка.
            # В остальное время нет пути, из которого UrBackup мог бы
            # автоматически сохранить пустой каталог вместо файлов NAS.
            run(CLIENT, "add-backupdir", "-n", "nas-data", "-d", str(STAGING))
            configured = True
            if str(STAGING) not in run(CLIENT, "list-backupdirs"):
                raise SnapshotError("Клиент не подтвердил включение пути ZFS snapshot")
            run(CLIENT, "start", "-f", timeout=21600)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if list_backups() - previous:
                    print("Файловая копия NAS завершена и записана в историю UrBackup")
                    return 0
                time.sleep(5)
            raise SnapshotError("UrBackup не подтвердил новую точку восстановления")
        finally:
            if configured:
                # Если клиент занят, не уничтожать снимок вслепую.
                run(CLIENT, "remove-backupdir", "-n", "nas-data")
            if mounted:
                # Никогда не удалять снимок, пока его читает резервная задача.
                # При проблеме unmount (busy) оставляем снимок и сообщаем об ошибке.
                try:
                    run("umount", str(STAGING), timeout=90)
                    mounted = False
                except SnapshotError:
                    raise SnapshotError("Снимок занят клиентом; отказ от его удаления")
            if not mounted and STAGING.exists():
                STAGING.rmdir()
            if created and not mounted:
                run("zfs", "destroy", snapshot)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SnapshotError, BlockingIOError, OSError, ValueError) as exc:
        print("ОШИБКА UrBackup NAS: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
