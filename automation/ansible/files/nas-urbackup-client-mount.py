#!/usr/bin/env python3
"""Idempotent Docker mount-propagation guard for NAS ZFS file snapshots.

The host owns ZFS and all mounts. Docker sees a read-only rslave subtree.
No device is passed to the client. No HDD layout or user files are changed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

STAGING = Path("/run/nas-urbackup-source")
PREFLIGHT = "/usr/local/sbin/nas-urbackup-safe"


class MountError(Exception):
    pass


def run(*args: str) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MountError("Недоступна операция подготовки mount") from exc
    if p.returncode:
        raise MountError("Ошибка подготовки mount: " + args[0])
    return p.stdout.strip()


def check() -> None:
    if STAGING.is_symlink() or not STAGING.is_dir():
        raise MountError("Нет доверенного родительского каталога ZFS снимков")
    if not os.path.ismount(str(STAGING)):
        raise MountError("Docker staging не является самостоятельным mountpoint")
    if run("findmnt", "-n", "-M", str(STAGING), "-o", "PROPAGATION") != "shared":
        raise MountError("ZFS staging не имеет mount propagation shared")


def prepare() -> None:
    run(PREFLIGHT, "preflight")
    if STAGING.is_symlink() or (STAGING.exists() and not STAGING.is_dir()):
        raise MountError("Недопустимый каталог staging")
    STAGING.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STAGING, 0o700)
    if not os.path.ismount(str(STAGING)):
        # Self bind is required: ordinary directories within /run do not
        # automatically become shared propagation roots for Docker rslave.
        run("mount", "--bind", str(STAGING), str(STAGING))
    run("mount", "--make-rshared", str(STAGING))
    check()


def main() -> int:
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2:
            raise MountError("Подготовка требует root и одного действия")
        if sys.argv[1] == "prepare":
            prepare()
        elif sys.argv[1] == "check":
            run(PREFLIGHT, "preflight")
            check()
        else:
            raise MountError("Неизвестное действие")
        return 0
    except MountError as exc:
        print("ОШИБКА UrBackup Docker mount: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
