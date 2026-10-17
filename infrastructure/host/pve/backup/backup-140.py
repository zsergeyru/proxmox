#!/usr/bin/env python3
"""Согласованная зашифрованная копия состояния 140 через restic.

Запускается на PVE только с явно заданными и уже подключёнными назначениями.
Не восстанавливает данные и не удаляет старые копии автоматически.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

DEFAULT_SOURCE = Path("/mnt/bindmounts/infra-state/140-web-gateway")
DATABASES = (
    Path("authelia/db.sqlite3"),
    Path("homarr/db/db.sqlite"),
)


def check_source(root: Path) -> None:
    if root.is_symlink() or not root.is_dir() or not root.parent.is_mount():
        raise RuntimeError("Том infra_state не подключён; резервирование запрещено")
    required = (root / "secrets/.initialized", root / "authelia/users_database.yml", *(
        root / rel for rel in DATABASES
    ))
    for path in required:
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Неполные постоянные данные: " + str(path))


def check_destination(root: Path) -> None:
    if root.is_symlink() or not root.is_dir() or not root.is_mount():
        raise RuntimeError("Место для копии не подключено: " + str(root))
    if not os.access(root, os.W_OK):
        raise RuntimeError("Место для копии недоступно для записи")


def consistent_copy(source: Path, target: Path) -> None:
    """Скопировать файлы, базы SQLite снять штатным механизмом online backup."""
    ignored = {rel.as_posix() for rel in DATABASES}
    stage = target / "140-web-gateway"
    def _ignore(current, names):
        relative = Path(current).relative_to(source)
        return [
            name for name in names
            if (relative / name).as_posix() in ignored
            or name.endswith(("-wal", "-shm"))
        ]
    shutil.copytree(source, stage, symlinks=False, ignore=_ignore)
    for rel in DATABASES:
        destination = stage / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source / rel}?mode=ro", uri=True) as original:
            with sqlite3.connect(destination) as snapshot:
                original.backup(snapshot)
                result = snapshot.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError("Повреждён снимок базы: " + rel.as_posix())
    metadata = {
        "guest": 140,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "sqlite_databases": [str(x) for x in DATABASES],
        "restoration": "Ручное восстановление; развёртывание никогда не восстанавливает копии",
    }
    (target / "manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def backup(source: Path, destinations: list[Path], password_file: Path) -> None:
    check_source(source)
    if len(destinations) != 2 or len({p.resolve() for p in destinations}) != 2:
        raise RuntimeError("Нужны два разных назначения: сетевое и съёмное")
    for path in destinations:
        check_destination(path)
    if password_file.is_symlink() or not password_file.is_file():
        raise RuntimeError("Отсутствует защищённый пароль резервных копий")
    if password_file.stat().st_mode & 0o077:
        raise RuntimeError("Пароль должен быть доступен только администратору")
    if shutil.which("restic") is None:
        raise RuntimeError("Не установлен restic")
    with tempfile.TemporaryDirectory(prefix="backup-140-", dir="/var/tmp") as workspace:
        stage = Path(workspace)
        os.chmod(stage, 0o700)
        consistent_copy(source, stage)
        for destination in destinations:
            env = dict(os.environ, RESTIC_REPOSITORY=str(destination / "guest-140"),
                       RESTIC_PASSWORD_FILE=str(password_file))
            # Репозитории должны быть подготовлены заранее: не инициализировать
            # произвольную папку без подтверждённого носителя.
            subprocess.run(["restic", "snapshots", "--json"], env=env,
                           check=True, capture_output=True)
            subprocess.run(["restic", "backup", str(stage),
                            "--tag", "guest-140", "--tag", "infra-state"],
                           env=env, check=True, capture_output=True)
            subprocess.run(["restic", "check", "--read-data-subset=1%"],
                           env=env, check=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--network", required=True, type=Path)
    parser.add_argument("--usb", required=True, type=Path)
    parser.add_argument("--password-file", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Запускать от администратора PVE")
    backup(args.source, [args.network, args.usb], args.password_file)
    print("Гость 140: две зашифрованные копии созданы и проверены")


if __name__ == "__main__":
    main()
