#!/usr/bin/env python3
"""PVE-only аварийный helper управляющего контура 910."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
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
LEGACY_ACCESS_GITHUB_DIR = ACCESS_DIR / "github"
LEGACY_ACCESS_GITHUB_KEY = LEGACY_ACCESS_GITHUB_DIR / "github_proxmox_repo_ed25519"
LEGACY_ACCESS_PVE_API_DIR = ACCESS_DIR / "pve-api"
LEGACY_STATE_SECRETS_DIR = STATE_DIR / "secrets"

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


def ensure_directory(
    path: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def prepare_git_directories() -> None:
    """Зафиксировать владельцев и права каталогов recovery Git."""
    ensure_directory(RECOVERY_DIR, mode=0o700, uid=0, gid=0)
    ensure_directory(BOOTSTRAP_DIR, mode=0o700, uid=0, gid=0)


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
        BOOTSTRAP_GITHUB_KEY,
        LEGACY_ACCESS_GITHUB_KEY,
    ):
        if nonempty(candidate):
            return candidate
    raise RecoveryError(
        "Не найден ни один экземпляр GitHub Deploy Key для восстановления"
    )


def prepare_git_recovery() -> None:
    """Синхронизировать каноническую recovery- и bootstrap-копии Git key."""
    source = _select_git_source()

    for candidate in (
        RECOVERY_GITHUB_KEY,
        BOOTSTRAP_GITHUB_KEY,
        LEGACY_ACCESS_GITHUB_KEY,
    ):
        if nonempty(candidate) and not same_content(source, candidate):
            raise RecoveryError(
                "Найдены разные GitHub Deploy Key; автоматическая замена запрещена: "
                f"{candidate}"
            )

    prepare_git_directories()
    atomic_copy(source, RECOVERY_GITHUB_KEY, mode=0o600, uid=0, gid=0)
    atomic_copy(source, BOOTSTRAP_GITHUB_KEY, mode=0o600, uid=0, gid=0)


def restore_git_access() -> None:
    """Восстановить bootstrap Git key только из PVE-only recovery."""
    if not nonempty(RECOVERY_GITHUB_KEY):
        raise RecoveryError(
            f"Отсутствует аварийная копия GitHub Deploy Key: {RECOVERY_GITHUB_KEY}"
        )
    prepare_git_directories()
    atomic_copy(
        RECOVERY_GITHUB_KEY,
        BOOTSTRAP_GITHUB_KEY,
        mode=0o600,
        uid=0,
        gid=0,
    )


def managed_guests_exist() -> bool:
    """Вернуть True, если PVE pool managed содержит объекты."""
    result = subprocess.run(
        [
            "pvesh",
            "get",
            "/pools/managed",
            "--output-format",
            "json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().lower()
        if "does not exist" in detail or "not found" in detail:
            return False
        raise RecoveryError(
            "Не удалось проверить состав PVE pool managed: "
            + ((result.stderr or "").strip() or f"код {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RecoveryError(
            "PVE вернул некорректный JSON для pool managed"
        ) from exc
    members = payload.get("members") if isinstance(payload, dict) else None
    if members is None:
        return False
    if not isinstance(members, list):
        raise RecoveryError("PVE pool managed имеет некорректный members")
    return bool(members)


def verify_recovery_state(*, require_approle: bool = True) -> None:
    required_dirs = [
        PVE_ONLY_DIR,
        STATE_DIR,
        OPENBAO_RAFT_DIR,
        RECOVERY_DIR,
    ]
    missing_dirs = [str(path) for path in required_dirs if not path.is_dir()]
    if missing_dirs:
        raise RecoveryError(
            "Отсутствуют обязательные recovery-каталоги: "
            + ", ".join(missing_dirs)
        )

    required_files = [
        RECOVERY_GITHUB_KEY,
        OPENBAO_UNSEAL_KEY,
        SEMAPHORE_DB,
    ]
    if require_approle:
        required_files.extend(
            (
                OPENBAO_SSH_ACCESS,
                OPENBAO_KV_ACCESS,
            )
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

    if managed_guests_exist() and not nonempty(OPENTOFU_STATE):
        raise RecoveryError(
            "В pool managed есть объекты, но постоянный OpenTofu state отсутствует"
        )


def cleanup_transition_state() -> None:
    """Удалить старые файловые secret-источники после полной проверки."""

    verify_recovery_state(require_approle=True)
    if nonempty(LEGACY_ACCESS_GITHUB_KEY) and not same_content(
        RECOVERY_GITHUB_KEY,
        LEGACY_ACCESS_GITHUB_KEY,
    ):
        raise RecoveryError(
            "Старый access Git key отличается от recovery-копии; "
            "автоматическое удаление запрещено"
        )

    for path in (
        LEGACY_ACCESS_GITHUB_DIR,
        LEGACY_ACCESS_PVE_API_DIR,
        LEGACY_STATE_SECRETS_DIR,
    ):
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


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
        "--preflight",
        action="store_true",
        help="Проверить минимальное состояние перед восстановлением",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Проверить полную готовность recovery-контура",
    )
    mode.add_argument(
        "--cleanup-transition",
        action="store_true",
        help="Удалить проверенные переходные файловые secret-источники",
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
            elif args.preflight:
                verify_recovery_state(require_approle=False)
                print("[ОК] Минимальное recovery-состояние 910 сохранно")
            elif args.cleanup_transition:
                cleanup_transition_state()
                print("[ОК] Переходные файловые secret-источники удалены")
            else:
                verify_recovery_state(require_approle=True)
                print("[ОК] Аварийный контур 910 готов")
    except (OSError, RecoveryError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
