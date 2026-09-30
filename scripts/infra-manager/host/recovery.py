#!/usr/bin/env python3
"""PVE-only аварийный helper управляющего контура 910."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sys
import tempfile
from pathlib import Path

PERSISTENT_ROOT = Path("/mnt/bindmounts/infra-manager")
PVE_ONLY_DIR = PERSISTENT_ROOT / "pve-only"
ACCESS_DIR = PERSISTENT_ROOT / "access"
STATE_DIR = PERSISTENT_ROOT / "state"

RECOVERY_DIR = PVE_ONLY_DIR / "recovery"
RECOVERY_GITHUB_KEY = RECOVERY_DIR / "github_proxmox_repo_ed25519"

BOOTSTRAP_DIR = Path("/root/.config/proxmox-bootstrap")
BOOTSTRAP_GITHUB_KEY = BOOTSTRAP_DIR / "github_proxmox_repo_ed25519"
ACCESS_GITHUB_KEY = ACCESS_DIR / "github" / "github_proxmox_repo_ed25519"

OPENBAO_DIR = PVE_ONLY_DIR / "openbao"
OPENBAO_UNSEAL_KEY = OPENBAO_DIR / "unseal.key"
OPENBAO_SSH_ACCESS = OPENBAO_DIR / "ssh-access.json"
OPENBAO_KV_ACCESS = OPENBAO_DIR / "kv-access.json"

OPENBAO_RAFT_DIR = STATE_DIR / "openbao" / "raft"
OPENTOFU_STATE = STATE_DIR / "opentofu" / "state" / "proxmox.tfstate"
SEMAPHORE_DB = STATE_DIR / "semaphore" / "semaphore.sqlite"

LOCK_PATH = Path("/run/lock/infra-manager-recovery.lock")


class RecoveryError(RuntimeError):
    pass


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def directory_has_files(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(item.is_file() and item.stat().st_size > 0 for item in path.rglob("*"))


def atomic_copy(
    source: Path,
    target: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    if not nonempty(source):
        raise RecoveryError(f"Отсутствует исходный файл: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(name)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def same_content(first: Path, second: Path) -> bool:
    if not nonempty(first) or not nonempty(second):
        return False
    return first.read_bytes() == second.read_bytes()


def _select_git_source() -> Path:
    for candidate in (
        RECOVERY_GITHUB_KEY,
        ACCESS_GITHUB_KEY,
        BOOTSTRAP_GITHUB_KEY,
    ):
        if nonempty(candidate):
            return candidate
    raise RecoveryError(
        "Не найден ни один экземпляр GitHub Deploy Key для восстановления"
    )


def prepare_git_recovery() -> None:
    """Синхронизировать аварийную, bootstrap- и access-копии Git key."""
    source = _select_git_source()

    for candidate in (
        RECOVERY_GITHUB_KEY,
        ACCESS_GITHUB_KEY,
        BOOTSTRAP_GITHUB_KEY,
    ):
        if nonempty(candidate) and not same_content(source, candidate):
            raise RecoveryError(
                "Найдены разные GitHub Deploy Key; автоматическая замена запрещена: "
                f"{candidate}"
            )

    atomic_copy(source, RECOVERY_GITHUB_KEY, mode=0o600, uid=0, gid=0)
    atomic_copy(source, BOOTSTRAP_GITHUB_KEY, mode=0o600, uid=0, gid=0)
    atomic_copy(
        source,
        ACCESS_GITHUB_KEY,
        mode=0o600,
        uid=100000,
        gid=100000,
    )


def restore_git_access() -> None:
    """Восстановить bootstrap/access-копии только из PVE-only recovery."""
    if not nonempty(RECOVERY_GITHUB_KEY):
        raise RecoveryError(
            f"Отсутствует аварийная копия GitHub Deploy Key: {RECOVERY_GITHUB_KEY}"
        )
    atomic_copy(
        RECOVERY_GITHUB_KEY,
        BOOTSTRAP_GITHUB_KEY,
        mode=0o600,
        uid=0,
        gid=0,
    )
    atomic_copy(
        RECOVERY_GITHUB_KEY,
        ACCESS_GITHUB_KEY,
        mode=0o600,
        uid=100000,
        gid=100000,
    )


def verify_recovery_state() -> None:
    required_dirs = (
        PVE_ONLY_DIR,
        ACCESS_DIR,
        STATE_DIR,
        OPENBAO_RAFT_DIR,
        RECOVERY_DIR,
    )
    missing_dirs = [str(path) for path in required_dirs if not path.is_dir()]
    if missing_dirs:
        raise RecoveryError(
            "Отсутствуют обязательные recovery-каталоги: "
            + ", ".join(missing_dirs)
        )

    required_files = (
        RECOVERY_GITHUB_KEY,
        OPENBAO_UNSEAL_KEY,
        OPENBAO_SSH_ACCESS,
        OPENBAO_KV_ACCESS,
        OPENTOFU_STATE,
        SEMAPHORE_DB,
    )
    missing_files = [
        str(path)
        for path in required_files
        if not nonempty(path)
    ]
    if missing_files:
        raise RecoveryError(
            "Отсутствуют обязательные recovery-файлы: "
            + ", ".join(missing_files)
        )

    if not directory_has_files(OPENBAO_RAFT_DIR):
        raise RecoveryError(
            "Raft-состояние OpenBao отсутствует или пусто"
        )


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    stream = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise RecoveryError(
            "Другой процесс уже изменяет recovery-контур"
        ) from exc
    return stream


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PVE-only аварийный контур infra-manager"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Подготовить аварийную Git-копию",
    )
    mode.add_argument(
        "--restore-git-access",
        action="store_true",
        help="Восстановить bootstrap/access Git key из pve-only",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Только проверить готовность recovery-контура",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ОШИБКА: recovery helper должен выполняться от root на PVE", file=sys.stderr)
        return 1

    try:
        with acquire_lock():
            if args.prepare:
                prepare_git_recovery()
                print("[ОК] Аварийная Git-копия 910 подготовлена")
            elif args.restore_git_access:
                restore_git_access()
                print("[ОК] Bootstrap Git-доступ восстановлен из PVE-only recovery")
            else:
                verify_recovery_state()
                print("[ОК] Аварийный контур 910 готов")
    except (OSError, RecoveryError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
