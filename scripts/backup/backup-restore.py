#!/usr/bin/env python3
"""Проверка и изолированное извлечение копии 910/PVE без замены рабочих файлов."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("backup_core_restore", BASE / "backup-pve.py")
assert spec and spec.loader
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def check_target(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise core.BackupError("Нужен существующий обычный каталог проверки")
    if any(path.iterdir()):
        raise core.BackupError("Каталог для пробного восстановления должен быть пустым")
    if path == Path("/") or path.is_relative_to(Path("/etc")) or path.is_relative_to(
        Path("/mnt/bindmounts")
    ) or path.is_relative_to(Path("/var/lib")):
        raise core.BackupError("Запрещено восстанавливать поверх рабочего PVE/910")
    if path.stat().st_mode & 0o077:
        raise core.BackupError("Тестовый каталог с секретами должен иметь права 0700")


def check_snapshots(repo: Path, password: Path) -> None:
    core.restic_command(repo, password, ["snapshots", "--tag", "infra-core", "--json"])
    core.restic_command(repo, password, ["check"])


def restore_into(repo: Path, password: Path, target: Path, confirmation: str) -> None:
    check_target(target)
    if confirmation != "ИЗОЛИРОВАННО":
        raise core.BackupError("Нужно подтверждение изолированного восстановления")
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo), RESTIC_PASSWORD_FILE=str(password))
    outcome = subprocess.run(
        [
            "restic", "restore", "latest", "--tag", "infra-core",
            "--target", str(target),
        ],
        env=env, capture_output=True, text=True, check=False, timeout=7200,
    )
    if outcome.returncode:
        raise core.BackupError("Пробное извлечение restic завершилось ошибкой")
    required = (
        "etc/pve",
        "mnt/bindmounts/infra-manager/pve-only",
        "mnt/bindmounts/infra-manager/access",
        "mnt/bindmounts/infra-manager/state/opentofu",
    )
    if any(not (target / p).exists() for p in required):
        raise core.BackupError("В пробном восстановлении отсутствует обязательная область")
    snapshots = list(target.rglob("openbao-raft.snap"))
    sqlite = list(target.rglob("semaphore.sqlite"))
    if len(snapshots) != 1 or len(sqlite) != 1:
        raise core.BackupError("Нет согласованного снимка OpenBao или Semaphore")
    print("[ОК] Копия извлечена отдельно от рабочих данных")
    print("Следующий этап: запуск служб только в изолированной проверочной среде")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", choices=("fast-ssd", "nas-backup"), required=True)
    p.add_argument("--extract-to", type=Path)
    p.add_argument("--confirm", default="")
    a = p.parse_args()
    if os.geteuid() != 0:
        p.error("Только root на физическом PVE")
    try:
        config = core.load_config(core.DEFAULT_CONFIG)
        destinations = core.check_destinations(config["destinations"])
        password = Path(config["password_file"])
        core._regular_file(password, secret=True)
        index = {"fast-ssd": 0, "nas-backup": 1}[a.repo]
        check_snapshots(destinations[index], password)
        if a.extract_to:
            restore_into(destinations[index], password, a.extract_to, a.confirm)
        else:
            print(f"[ОК] Зашифрованный архив {a.repo} доступен и прошёл restic check")
        return 0
    except (core.BackupError, OSError, ValueError, KeyError) as exc:
        print(f"[ОШИБКА] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
