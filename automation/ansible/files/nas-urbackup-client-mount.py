#!/usr/bin/env python3
"""Safely prepare Docker rslave propagation for ZFS snapshots on NAS.

A self bind mount may be on the same filesystem as its parent. Python
os.path.ismount() cannot reliably detect such mounts; use exact findmnt -M.
The source is always a dedicated /run directory, never a physical HDD.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

STAGING = Path("/run/nas-urbackup-source")
PREFLIGHT = "/usr/local/sbin/nas-urbackup-safe"

# Only predefined stage codes are printed by this root-owned helper. Ansible
# maps them to human-readable reasons; raw command output is never exposed.
STAGES = {
    "preflight": "Проверка физических ZFS-пулов не прошла",
    "directory": "Недопустимый или недоступный каталог staging",
    "occupied": "Каталог staging не пуст: монтирование поверх данных запрещено",
    "inspect": "Не удалось достоверно прочитать таблицу mountpoint",
    "bind": "Не удалось создать отдельный bind mount для staging",
    "propagation": "Не удалось включить shared propagation на staging",
    "missing": "Точка staging отсутствует в таблице mountpoint",
    "not-shared": "Точка staging не получила shared propagation",
}


class MountError(Exception):
    def __init__(self, stage: str):
        if stage not in STAGES:
            raise ValueError("Unknown mount stage")
        self.stage = stage
        super().__init__(STAGES[stage])


def run(*args: str) -> str:
    """Execute fixed local commands without displaying arbitrary stderr."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        stage = ("preflight" if args[0] == PREFLIGHT else
                 "bind" if args[:2] == ("mount", "--bind") else
                 "propagation" if args[:2] == ("mount", "--make-rshared") else
                 "inspect")
        raise MountError(stage) from exc
    if p.returncode:
        stage = ("preflight" if args[0] == PREFLIGHT else
                 "bind" if args[:2] == ("mount", "--bind") else
                 "propagation" if args[:2] == ("mount", "--make-rshared") else
                 "inspect")
        raise MountError(stage)
    return p.stdout.strip()


def is_mountpoint() -> bool:
    """Use findmnt's *exact* mountpoint lookup; not os.path.ismount()."""
    try:
        p = subprocess.run(
            ("findmnt", "-n", "-M", str(STAGING), "-o", "TARGET"),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MountError("inspect") from exc
    # No entry is expected before the first prepare.
    if p.returncode == 1 and not p.stdout.strip():
        return False
    if p.returncode or p.stdout.strip() != str(STAGING):
        raise MountError("inspect")
    return True


def check() -> None:
    if STAGING.is_symlink() or not STAGING.is_dir():
        raise MountError("directory")
    if not is_mountpoint():
        raise MountError("missing")
    if run("findmnt", "-n", "-M", str(STAGING), "-o", "PROPAGATION") != "shared":
        raise MountError("not-shared")


def prepare() -> None:
    run(PREFLIGHT, "preflight")
    if STAGING.is_symlink() or (STAGING.exists() and not STAGING.is_dir()):
        raise MountError("directory")
    try:
        STAGING.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(STAGING, 0o700)
    except OSError as exc:
        raise MountError("directory") from exc
    if not is_mountpoint():
        # The directory is initially empty. Refuse to hide anything in it.
        try:
            if any(STAGING.iterdir()):
                raise MountError("occupied")
        except OSError as exc:
            raise MountError("directory") from exc
        run("mount", "--bind", str(STAGING), str(STAGING))
    # Safe to run repeatedly on a pre-existing self bind mount.
    run("mount", "--make-rshared", str(STAGING))
    check()


def main() -> int:
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2:
            raise MountError("preflight")
        if sys.argv[1] == "prepare":
            prepare()
        elif sys.argv[1] == "check":
            run(PREFLIGHT, "preflight")
            check()
        else:
            raise MountError("preflight")
        return 0
    except MountError as exc:
        # Safe machine-readable output: no paths, credentials or tool stderr.
        print("NAS_URBACKUP_MOUNT_STAGE=" + exc.stage, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
