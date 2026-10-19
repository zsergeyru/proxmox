#!/usr/bin/env python3
"""Проверяемое зашифрованное резервирование состояния PVE и 910.

Запускается только на физическом PVE от root. Не размечает накопители,
не подключает сети, не создаёт restic-хранилища и не удаляет старые копии.
До подтверждения двух физических носителей ничего не записывает.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any

import yaml


DEFAULT_CONFIG = Path("/etc/infra-manager/backup/backup.yaml")
RAFT_HELPER = Path("/usr/local/lib/infra-manager/backup/openbao-raft-guest.py")
STATE = Path("/mnt/bindmounts/infra-manager/state")
ROOT = Path("/mnt/bindmounts/infra-manager")
SEMAPHORE_DB = STATE / "semaphore/semaphore.sqlite"
OPENTOFU_STATE = STATE / "opentofu/state/proxmox.tfstate"
ACTIVATION = STATE / "semaphore/.infra-manager-runtime-activation-pending"
LOCK = STATE / "locks/910.lock"
SOURCES = (
    Path("/etc/pve"),
    Path("/etc/network/interfaces"),
    Path("/etc/fstab"),
    Path("/etc/hosts"),
    Path("/etc/infra-manager"),
    ROOT / "pve-only",
    ROOT / "access",
    STATE / "opentofu",
    STATE / "physical-hosts",
    STATE / "semaphore",
)
REQUIRED = (
    ROOT / "pve-only/openbao/unseal.key",
    ROOT / "pve-only/recovery/github_proxmox_repo_ed25519",
    SEMAPHORE_DB,
    OPENTOFU_STATE,
)


class BackupError(RuntimeError):
    """Понятная причина отказа без разглашения секретов."""


def _regular_file(path: Path, *, secret: bool = False) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise BackupError(f"Обязательный файл отсутствует или небезопасен: {path}")
    if secret and path.stat().st_mode & 0o077:
        raise BackupError(f"Права секретного файла слишком широкие: {path}")


def load_config(path: Path) -> dict[str, Any]:
    """Прочитать единственный поддерживаемый машинный формат без выполнения YAML."""

    if path.is_symlink() or not path.is_file():
        raise BackupError(f"Нет защищённого описания резервирования: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise BackupError("Требуется версия 1 backup.yaml")
    if raw.get("host") != "pve" or raw.get("job") != "infra-core":
        raise BackupError("Описание резервирования предназначено не для этого PVE")
    targets = raw.get("destinations")
    if not isinstance(targets, list) or len(targets) != 2:
        raise BackupError("Нужны два независимых назначения: SSD и NAS")
    if [t.get("id") for t in targets if isinstance(t, dict)] != [
        "fast-ssd", "nas-backup",
    ]:
        raise BackupError("Неожиданный состав назначений")
    return raw


def _mounted_device(path: Path) -> dict[str, str]:
    """Проверить именно mountpoint, не принимая обычную папку за диск."""

    if path.is_symlink() or not path.is_dir() or not path.is_mount():
        raise BackupError(f"Носитель не подключён по адресу {path}")
    result = subprocess.run(
        ["findmnt", "--json", "-M", str(path), "-o", "TARGET,SOURCE,FSTYPE,UUID"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise BackupError(f"Не удалось определить носитель {path}")
    try:
        entries = json.loads(result.stdout)["filesystems"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError("Неполный ответ findmnt") from exc
    if len(entries) != 1 or entries[0].get("target") != str(path):
        raise BackupError(f"Найдено неожиданное подключение {path}")
    return entries[0]


def check_destinations(targets: list[dict[str, Any]]) -> list[Path]:
    """Перед любой записью сверить смонтированные и разные физические тома."""

    result: list[Path] = []
    devices: list[str] = []
    for entry in targets:
        mount = Path(entry["mountpoint"])
        if not mount.is_absolute() or mount == Path("/") or mount.is_symlink():
            raise BackupError("Назначение должно быть абсолютной точкой подключения")
        required_type = "ext4" if entry["id"] == "fast-ssd" else "nfs4"
        if entry.get("filesystem") != required_type:
            raise BackupError(
                f"Для {entry['id']} ожидается файловая система {required_type}"
            )
        uuid = entry.get("uuid")
        source = entry.get("source")
        if not uuid and not source:
            raise BackupError(
                f"Диск {entry['id']} пока не подтверждён; "
                "заполните UUID или источник в backup.yaml после проверки"
            )
        actual = _mounted_device(mount)
        if actual.get("fstype") != entry["filesystem"]:
            raise BackupError(f"Неверная файловая система у {entry['id']}")
        if uuid and actual.get("uuid") != uuid:
            raise BackupError(f"UUID диска {entry['id']} не совпадает")
        if source and actual.get("source") != source:
            raise BackupError(f"Источник диска {entry['id']} не совпадает")
        physical = str(actual.get("source") or "")
        if not physical or physical in devices:
            raise BackupError("Два назначения находятся на одном источнике")
        devices.append(physical)
        repository = entry.get("repository")
        if not isinstance(repository, str) or not repository or "/" in repository:
            raise BackupError("Некорректное имя restic-хранилища")
        folder = mount / repository
        if folder.is_symlink() or not folder.is_dir():
            raise BackupError(f"Restic-хранилище не подготовлено: {folder}")
        _regular_file(folder / "config")
        result.append(folder)
    return result


def check_source() -> None:
    """Проверить критичные PVE-only, Semaphore, OpenTofu и конфигурацию PVE."""

    if not Path("/etc/pve").is_mount():
        raise BackupError("/etc/pve не подключён; копия PVE небезопасна")
    for source in SOURCES:
        if source.is_symlink() or not source.exists():
            raise BackupError(f"Нет исходных данных: {source}")
    for required in REQUIRED:
        _regular_file(required)
    if not (STATE / "openbao/raft").is_dir():
        raise BackupError("Не найдено постоянное Raft-хранилище OpenBao")
    if ACTIVATION.exists():
        raise BackupError("Идёт активация 910: резервирование отложено")


@contextlib.contextmanager
def operation_guard():
    """Не читать OpenTofu и секреты одновременно с изменяющим deploy 910."""

    if LOCK.is_symlink() or not LOCK.parent.is_dir():
        raise BackupError("Постоянные блокировки 910 не подготовлены")
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("910 занят изменяющей операцией") from exc
        if ACTIVATION.exists():
            raise BackupError("Самообновление 910 ещё не завершено")
        yield
    finally:
        os.close(fd)


def sqlite_snapshot(target: Path) -> None:
    """Снять согласованную копию SQLite через встроенный интерфейс backup."""

    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(f"file:{SEMAPHORE_DB}?mode=ro", uri=True)
    try:
        with sqlite3.connect(target) as destination:
            origin.backup(destination)
            check = destination.execute("PRAGMA integrity_check").fetchone()
            if check != ("ok",):
                raise BackupError("Снимок базы Semaphore не прошёл проверку")
    finally:
        origin.close()
    target.chmod(0o600)


def check_openbao_credentials(config: dict[str, Any]) -> dict[str, str]:
    """Проверить PVE-only учётные данные и принадлежность управляющего LXC."""

    if not isinstance(config, dict):
        raise BackupError("Не описан источник снимка OpenBao")
    role_path = Path(config["approle_file"])
    identity_path = Path(config["identity_file"])
    _regular_file(role_path, secret=True)
    _regular_file(identity_path)
    if not role_path.is_relative_to(ROOT / "pve-only"):
        raise BackupError("AppRole OpenBao должен храниться только в PVE-only")
    try:
        role = json.loads(role_path.read_text(encoding="utf-8"))
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BackupError("Неверные данные доступа OpenBao") from exc
    if not isinstance(role, dict) or set(role) != {"role_id", "secret_id"}:
        raise BackupError("Нужен отдельный AppRole только для снимка Raft")
    if not all(isinstance(v, str) and v for v in role.values()):
        raise BackupError("AppRole для снимка Raft неполон")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("vmid"), int)
        or identity["vmid"] <= 0
        or identity.get("name") != "infra-manager"
    ):
        raise BackupError("Управляющий LXC не подтверждён в PVE")
    vmid = identity["vmid"]
    info = subprocess.run(
        ["pct", "config", str(vmid)], capture_output=True, text=True, check=False,
    )
    required = (
        "hostname: infra-manager",
        "unprivileged: 1",
        "owner=proxmox-project",
        "role=infra-manager",
    )
    if info.returncode or any(value not in info.stdout for value in required):
        raise BackupError("Управляющий LXC не совпадает с доверенным описанием")
    return role


def openbao_snapshot(target: Path, config: dict[str, Any]) -> None:
    """Передать согласованный Raft-снимок из 910 на PVE без передачи unseal key."""

    role = check_openbao_credentials(config)
    identity = json.loads(Path(config["identity_file"]).read_text(encoding="utf-8"))
    _regular_file(RAFT_HELPER)
    code = RAFT_HELPER.read_text(encoding="utf-8")
    argv = [
        "pct", "exec", str(identity["vmid"]), "--",
        "python3", "-c", code,
    ]
    if target.exists() or target.is_symlink():
        raise BackupError("Временный файл снимка Raft уже существует")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        outcome = subprocess.run(
            argv,
            input=json.dumps(role), text=True, stdout=output,
            stderr=subprocess.PIPE, timeout=240, check=False,
        )
    if outcome.returncode:
        target.unlink(missing_ok=True)
        raise BackupError("Не удалось получить согласованный снимок OpenBao Raft")
    if target.stat().st_size < 256 or target.stat().st_size > 2 * 1024**3:
        target.unlink(missing_ok=True)
        raise BackupError("Размер снимка OpenBao Raft недопустим")
    target.chmod(0o600)


def restic_command(repo: Path, password_file: Path, argv: list[str]) -> None:
    """Передавать ключ restic только путём файла; вывод ошибок не раскрывать."""

    environment = dict(os.environ)
    environment.update({
        "RESTIC_REPOSITORY": str(repo),
        "RESTIC_PASSWORD_FILE": str(password_file),
    })
    outcome = subprocess.run(
        ["restic", *argv], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if outcome.returncode:
        raise BackupError(
            f"Restic ({repo.parent.name}, {argv[0]}) завершился "
            f"с кодом {outcome.returncode}; защищённый журнал храните вне Git"
        )


def backup(config: dict[str, Any], *, execute: bool = False) -> None:
    """Проверить все требования до начала записи в зашифрованные репозитории."""

    check_source()
    password_file = Path(config["password_file"])
    _regular_file(password_file, secret=True)
    targets = check_destinations(config["destinations"])
    check_openbao_credentials(config["openbao"])
    if shutil.which("restic") is None:
        raise BackupError("На PVE не установлен restic")
    for target in targets:
        restic_command(target, password_file, ["snapshots", "--json"])
    if not execute:
        print("[ОК] Оба независимых носителя и restic-хранилища подтверждены")
        return

    with operation_guard():
        with tempfile.TemporaryDirectory(prefix="infra-core-", dir="/run") as folder:
            stage = Path(folder)
            stage.chmod(0o700)
            consistent = stage / "consistent"
            consistent.mkdir(mode=0o700)
            sqlite_snapshot(consistent / "semaphore.sqlite")
            openbao_snapshot(consistent / "openbao-raft.snap", config["openbao"])
            metadata = {
                "kind": "infra-core",
                "host": "pve",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "consistency": "online SQLite backup + OpenBao Raft API snapshot",
                "restore": "Только вручную в изолированную среду",
            }
            (consistent / "manifest.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            restic_sources = [str(item) for item in SOURCES]
            exclusions = [
                "--exclude", str(SEMAPHORE_DB),
                "--exclude", str(SEMAPHORE_DB) + "-wal",
                "--exclude", str(SEMAPHORE_DB) + "-shm",
                "--exclude", str(STATE / "semaphore/*.sqlite*"),
                "--exclude", str(STATE / "semaphore/*.sqlite*-wal"),
                "--exclude", str(STATE / "semaphore/*.sqlite*-shm"),
                "--exclude", str(STATE / "opentofu/provider-mirror"),
                "--exclude", str(STATE / "opentofu/.terraform"),
                "--exclude", str(STATE / "opentofu/*.tfplan"),
            ]
            for target in targets:
                restic_command(
                    target, password_file,
                    ["backup", *restic_sources, str(consistent),
                     "--tag", "infra-core", "--tag", "pve", *exclusions],
                )
                restic_command(target, password_file, ["check", "--read-data-subset=1%"])
    print("[ОК] Два зашифрованных комплекта PVE и 910 записаны и проверены")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run", action="store_true", help="Создать две копии после проверок")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Резервирование запускается только root на PVE")
    try:
        backup(load_config(args.config), execute=args.run)
    except (BackupError, OSError, KeyError, ValueError, yaml.YAMLError) as exc:
        print(f"[ОШИБКА] Резервирование не выполнено: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
