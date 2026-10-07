#!/usr/bin/env python3
"""PVE-only аварийный helper управляющего контура infra-manager."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PERSISTENT_ROOT = Path("/mnt/bindmounts/infra-manager")
PVE_ONLY_DIR = PERSISTENT_ROOT / "pve-only"
ACCESS_DIR = PERSISTENT_ROOT / "access"
STATE_DIR = PERSISTENT_ROOT / "state"

RECOVERY_DIR = PVE_ONLY_DIR / "recovery"
RECOVERY_GITHUB_KEY = RECOVERY_DIR / "github_proxmox_repo_ed25519"

BOOTSTRAP_DIR = Path("/root/.config/proxmox-bootstrap")
BOOTSTRAP_GITHUB_KEY = BOOTSTRAP_DIR / "github_proxmox_repo_ed25519"

OPENBAO_DIR = PVE_ONLY_DIR / "openbao"
OPENBAO_UNSEAL_KEY = OPENBAO_DIR / "unseal.key"
OPENBAO_SSH_ACCESS = OPENBAO_DIR / "ssh-access.json"
OPENBAO_KV_ACCESS = OPENBAO_DIR / "kv-access.json"
OPENBAO_OPERATOR_ACCESS = OPENBAO_DIR / "operator-access.json"
OPENBAO_TLS_DIR = PVE_ONLY_DIR / "openbao-tls"
OPENBAO_TLS_CA_KEY = OPENBAO_TLS_DIR / "ca.key"
OPENBAO_TLS_CA_CERT = OPENBAO_TLS_DIR / "ca.crt"

OPENBAO_RAFT_DIR = STATE_DIR / "openbao" / "raft"
OPENTOFU_STATE = STATE_DIR / "opentofu" / "state" / "proxmox.tfstate"
SEMAPHORE_DB = STATE_DIR / "semaphore" / "semaphore.sqlite"

LOCK_PATH = Path("/run/lock/infra-manager-recovery.lock")
BOOTSTRAP_URL = (
    "https://raw.githubusercontent.com/"
    "zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh"
)


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


def otp_tls_initialized() -> bool:
    if not nonempty(OPENBAO_SSH_ACCESS):
        return False
    try:
        payload = json.loads(OPENBAO_SSH_ACCESS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "ssh-otp-config" in payload


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
                OPENBAO_OPERATOR_ACCESS,
            )
        )
        if otp_tls_initialized():
            required_files.extend(
                (
                    OPENBAO_TLS_CA_KEY,
                    OPENBAO_TLS_CA_CERT,
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


def _download_bootstrap(target: Path) -> None:
    try:
        with urllib.request.urlopen(BOOTSTRAP_URL, timeout=30) as response:
            payload = response.read(1024 * 1024 + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise RecoveryError(
            f"Не удалось получить bootstrap recovery: {exc}"
        ) from exc

    if not payload or len(payload) > 1024 * 1024:
        raise RecoveryError("Получен некорректный bootstrap recovery")
    if not payload.startswith(b"#!"):
        raise RecoveryError(
            "Bootstrap recovery не похож на исполняемый сценарий"
        )

    target.write_bytes(payload)
    target.chmod(0o700)


def run_recovery() -> None:
    """Запустить полный recovery после локальной проверки PVE-only состояния."""

    with acquire_lock():
        verify_recovery_state(require_approle=False)
        restore_git_access()

    print("[ОК] Минимальное recovery-состояние сохранно")
    print("[ОК] Bootstrap Git-доступ восстановлен из PVE-only recovery")

    with tempfile.TemporaryDirectory(
        prefix="infra-manager-recover."
    ) as temporary:
        bootstrap = Path(temporary) / "bootstrap-pve.sh"
        _download_bootstrap(bootstrap)
        result = subprocess.run(
            ["bash", str(bootstrap), "--recover"],
            check=False,
        )
    if result.returncode:
        raise RecoveryError(
            f"Bootstrap recovery завершился с кодом {result.returncode}"
        )


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
        help="Восстановить bootstrap Git key из pve-only",
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
        "--recover",
        action="store_true",
        help="Запустить полный аварийный bootstrap recovery",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("ОШИБКА: recovery helper должен выполняться от root на PVE", file=sys.stderr)
        return 1

    try:
        if args.recover:
            run_recovery()
            print("[ОК] Аварийное восстановление infra-manager завершено")
            return 0

        with acquire_lock():
            if args.prepare:
                prepare_git_recovery()
                print("[ОК] Аварийная Git-копия infra-manager подготовлена")
            elif args.restore_git_access:
                restore_git_access()
                print("[ОК] Bootstrap Git-доступ восстановлен из PVE-only recovery")
            elif args.preflight:
                verify_recovery_state(require_approle=False)
                print("[ОК] Минимальное recovery-состояние infra-manager сохранно")
            else:
                verify_recovery_state(require_approle=True)
                print("[ОК] Аварийный контур infra-manager готов")
    except (OSError, RecoveryError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
