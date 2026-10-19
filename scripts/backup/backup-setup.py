#!/usr/bin/env python3
"""Первоначальная подготовка и контроль расписания шифрованных архивов PVE."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("infra_backup_core", ROOT / "backup-pve.py")
assert SPEC is not None and SPEC.loader is not None
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)

CONFIRMATION = Path("/etc/infra-manager/backup/recovery-key-exported")
TIMER = "infra-manager-backup.timer"


def initial_password(path: Path) -> None:
    """Создать пароль один раз только на PVE, не печатая его и не перезаписывая."""

    if path.exists() or path.is_symlink():
        core._regular_file(path, secret=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(secrets.token_urlsafe(56) + "\n")
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def initialize() -> None:
    """Создать restic на двух заранее подтверждённых физических томах."""

    config = core.load_config(core.DEFAULT_CONFIG)
    core.check_source()
    core.check_openbao_credentials(config["openbao"])
    target_folders = core.check_destinations(
        config["destinations"], allow_uninitialized=True,
    )
    if shutil.which("restic") is None:
        raise core.BackupError("На PVE отсутствует restic")
    password_file = Path(config["password_file"])
    initial_password(password_file)
    for target in target_folders:
        if target.exists() and not target.is_dir():
            raise core.BackupError("Путь репозитория занят другим файлом")
        if target.is_symlink():
            raise core.BackupError("Ссылки на restic-репозитории запрещены")
        if not target.exists():
            target.mkdir(mode=0o700)
        if not (target / "config").exists():
            # Репозиторий допускается создавать только если каталог пуст.
            if any(target.iterdir()):
                raise core.BackupError(f"В каталоге {target} есть неизвестные данные")
            core.restic_command(target, password_file, ["init"])
        core.restic_command(target, password_file, ["snapshots", "--json"])
        core.restic_command(target, password_file, ["check"])
    print("[ОК] Оба restic-репозитория созданы или проверены")
    print("До включения расписания необходимо сохранить ключ на внешнем носителе")


def export_key(destination: Path) -> None:
    """Записать восстановительный пароль на явно смонтированный третий носитель."""

    config = core.load_config(core.DEFAULT_CONFIG)
    password = Path(config["password_file"])
    core._regular_file(password, secret=True)
    if destination.is_symlink() or destination.exists() or not destination.parent.is_mount():
        raise core.BackupError(
            "Ключ разрешено записать только новым файлом в корне "
            "отдельного подключённого носителя"
        )
    forbidden = [
        Path(item["mountpoint"]) for item in config["destinations"]
    ]
    if destination.parent in forbidden or destination.parent == Path("/"):
        raise core.BackupError("Третий носитель должен быть независимым от PVE/SSD/NAS")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            with password.open("rb") as source:
                shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if destination.read_bytes() != password.read_bytes():
            raise core.BackupError("Проверка экспортированного ключа не прошла")
        CONFIRMATION.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        CONFIRMATION.write_text(
            json.dumps({
                "exported_utc": datetime.now(timezone.utc).isoformat(),
                "destination_mount": str(destination.parent),
            }) + "\n", encoding="utf-8",
        )
        CONFIRMATION.chmod(0o600)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print("[ОК] Восстановительный ключ записан и проверен на независимом носителе")


def approve_restore(confirm: str) -> None:
    """Разрешить очистку только после реального испытания восстановления."""

    if confirm != "ПРОВЕРИЛ-ВОССТАНОВЛЕНИЕ":
        raise core.BackupError("Требуется явное подтверждение проверки восстановления")
    if not CONFIRMATION.is_file() or not core.LAST_SUCCESS.is_file():
        raise core.BackupError("Нет независимого ключа или успешного комплекта")
    config = core.load_config(core.DEFAULT_CONFIG)
    targets = core.check_destinations(config["destinations"])
    for target in targets:
        core.restic_command(target, Path(config["password_file"]), ["check"])
    core.RESTORE_APPROVED.parent.mkdir(parents=True, exist_ok=True)
    core.RESTORE_APPROVED.write_text(
        datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
    )
    core.RESTORE_APPROVED.chmod(0o600)
    print("[ОК] После пробного восстановления разрешено выполнять правила хранения")


def status() -> None:
    """Показать состояние, не раскрывая пароль и содержимое архивов."""

    if core.LAST_SUCCESS.exists():
        record = json.loads(core.LAST_SUCCESS.read_text(encoding="utf-8"))
        print("Последняя полная копия:", record.get("created_utc", "?"))
        print("Назначения:", ", ".join(record.get("destinations", [])))
    else:
        print("Успешных копий новой программы пока нет")
    print("Независимая копия ключа:", "подтверждена" if CONFIRMATION.is_file() else "не подтверждена")
    print("Испытание восстановления:", "подтверждено" if core.RESTORE_APPROVED.is_file() else "не подтверждено")
    r = subprocess.run(["systemctl", "is-enabled", TIMER], capture_output=True, text=True)
    print("Расписание:", "включено" if r.returncode == 0 else "выключено")


def enable() -> None:
    """Включить таймер только после первой успешной копии и вывоза ключа."""

    if not CONFIRMATION.is_file() or not core.LAST_SUCCESS.is_file():
        raise core.BackupError(
            "Сначала нужны независимая копия ключа и успешный пробный запуск"
        )
    config = core.load_config(core.DEFAULT_CONFIG)
    core.check_source()
    core.check_destinations(config["destinations"])
    core.check_openbao_credentials(config["openbao"])
    success = json.loads(core.LAST_SUCCESS.read_text(encoding="utf-8"))
    current = [str(p) for p in core.check_destinations(config["destinations"])]
    if success.get("destinations") != current:
        raise core.BackupError("Успешный архив получен на других носителях")
    subprocess.run(["systemctl", "enable", "--now", TIMER], check=True)
    print("[ОК] Периодическое резервирование включено")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    approved = sub.add_parser("approve-restore")
    approved.add_argument("--confirm", required=True)
    key = sub.add_parser("export-key")
    key.add_argument("--to", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Только root на PVE")
    try:
        if args.action == "init":
            initialize()
        elif args.action == "status":
            status()
        elif args.action == "export-key":
            export_key(args.to)
        elif args.action == "enable":
            enable()
        elif args.action == "approve-restore":
            approve_restore(args.confirm)
        else:
            subprocess.run(["systemctl", "disable", "--now", TIMER], check=True)
            print("[ОК] Периодическое резервирование выключено")
    except (core.BackupError, OSError, KeyError, ValueError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        print(f"[ОШИБКА] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
