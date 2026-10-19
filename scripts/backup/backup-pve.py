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
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any

import yaml


DEFAULT_CONFIG = Path("/etc/infra-manager/backup/backup.yaml")
LAST_SUCCESS = Path("/var/lib/infra-manager/backup/last-success.json")
RESTORE_APPROVED = Path("/etc/infra-manager/backup/restore-tested")
LAST_VM100 = Path("/var/lib/infra-manager/backup/last-vm100.json")
RAFT_HELPER = Path("/usr/local/lib/infra-manager/backup/openbao-raft-guest.py")
STATE = Path("/mnt/bindmounts/infra-manager/state")
ROOT = Path("/mnt/bindmounts/infra-manager")
SEMAPHORE_DB = STATE / "semaphore/semaphore.sqlite"
OPENTOFU_STATE = STATE / "opentofu/state/proxmox.tfstate"
ACTIVATION = STATE / "semaphore/.infra-manager-runtime-activation-pending"
LOCK = STATE / "locks/910.lock"
BACKUP_GATE = STATE / "locks/backup-gate.lock"
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


def check_destinations(
    targets: list[dict[str, Any]], *, allow_uninitialized: bool = False,
) -> list[Path]:
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
        if folder.is_symlink():
            raise BackupError(f"Ссылка вместо restic-хранилища: {folder}")
        if not allow_uninitialized:
            if not folder.is_dir():
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
    """Исключить изменяющие задания всех гостей на время копии 910."""

    for path in (BACKUP_GATE, LOCK):
        if path.is_symlink() or not path.is_file():
            raise BackupError(
                f"Нет общей блокировки {path}; сначала обновите управляющий контур"
            )
    barrier = os.open(BACKUP_GATE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(barrier, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("Задания infra-manager ещё выполняются") from exc
        descriptor = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupError("Управляющий гость занят изменениями") from exc
            if ACTIVATION.exists():
                raise BackupError("Самообновление 910 ещё не завершено")
            yield
        finally:
            os.close(descriptor)
    finally:
        os.close(barrier)



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


def _digest(path: Path) -> str:
    """Посчитать SHA256 на независимом носителе, не доверяя завершению копирования."""

    checksum = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def backup_vm100(config: dict[str, Any], targets: list[Path], password: Path) -> None:
    """Сохранить HAOS VM 100 в те же два шифрованных restic-хранилища."""

    guests = config.get("guests", [])
    if guests != [{
        "vmid": 100, "schedule": "daily",
        "mode": "snapshot", "compression": "zstd",
    }]:
        raise BackupError("Неподдерживаемый перечень гостей для резервирования")
    fingerprint = hashlib.sha256(json.dumps(
        config["destinations"], sort_keys=True,
    ).encode()).hexdigest()
    if LAST_VM100.is_file():
        try:
            previous = json.loads(LAST_VM100.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if (
            previous.get("date") == datetime.now(timezone.utc).date().isoformat()
            and previous.get("targets") == fingerprint
        ):
            return
    if shutil.which("vzdump") is None:
        raise BackupError("На физическом PVE нет vzdump")
    if shutil.which("qm") is None:
        raise BackupError("На физическом PVE нет qm")
    if subprocess.run(
        ["qm", "status", "100"], capture_output=True, text=True,
        check=False,
    ).returncode:
        raise BackupError("VM 100 отсутствует; резервирование невозможно")

    # Архив vzdump временно хранится на проверенном отдельном SSD, никогда
    # не на NVMe PVE. После включения в оба restic-хранилища удаляется.
    ssd_root = targets[0].parent
    with tempfile.TemporaryDirectory(prefix=".haos-vzdump-", dir=ssd_root) as tmp:
        workspace = Path(tmp)
        workspace.chmod(0o700)
        run = subprocess.run(
            [
                "vzdump", "100", "--mode", "snapshot",
                "--compress", "zstd", "--dumpdir", str(workspace),
            ],
            text=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=False, timeout=7200,
        )
        if run.returncode:
            raise BackupError("vzdump VM 100 завершился ошибкой")
        archives = list(workspace.glob("vzdump-qemu-100-*.vma.zst"))
        if len(archives) != 1 or archives[0].is_symlink():
            raise BackupError("vzdump не создал ровно один архив HAOS VM 100")
        archive = archives[0]
        if archive.stat().st_size < 512:
            raise BackupError("Пустой архив HAOS VM 100")
        for repo in targets:
            restic_command(
                repo, password,
                ["backup", str(archive), "--tag", "haos", "--tag", "vm-100"],
            )
            restic_command(repo, password, ["check", "--read-data-subset=1%"])
    LAST_VM100.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".vm100-", dir=LAST_VM100.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({
                "date": datetime.now(timezone.utc).date().isoformat(),
                "targets": fingerprint,
            }, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, LAST_VM100)
    finally:
        temporary.unlink(missing_ok=True)


def save_result(config: dict[str, Any], targets: list[Path]) -> None:
    """Зафиксировать подтверждённый успех только после обеих независимых копий."""

    LAST_SUCCESS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".last-success-", dir=LAST_SUCCESS.parent,
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "job": "infra-core",
                "destinations": [str(p) for p in targets],
                "guests": [guest["vmid"] for guest in config.get("guests", [])],
            }, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, LAST_SUCCESS)
    finally:
        temporary.unlink(missing_ok=True)


def retention(config: dict[str, Any], repos: list[Path], password: Path) -> None:
    """Удалять старые точки только после вручную проверенного восстановления.

    Время хранения задаётся политикой Git. Первая успешная копия и экспорт
    ключа сами по себе не разрешают необратимое удаление старых точек.
    """

    if not RESTORE_APPROVED.is_file():
        return
    _regular_file(RESTORE_APPROVED, secret=True)
    params = config.get("retention", {})
    if not isinstance(params, dict):
        raise BackupError("Нет политики хранения копий")
    fast = params.get("fast_ssd", {})
    nas = params.get("nas_backup", {})
    if (
        not isinstance(fast.get("keep_last"), int)
        or fast["keep_last"] < 2
        or not isinstance(nas.get("keep_daily"), int)
        or nas["keep_daily"] < 7
        or not isinstance(nas.get("keep_weekly"), int)
        or nas["keep_weekly"] < 4
    ):
        raise BackupError("Недостаточный срок хранения по политике")
    rules = (
        ["--keep-last", str(fast["keep_last"])],
        [
            "--keep-daily", str(nas["keep_daily"]),
            "--keep-weekly", str(nas["keep_weekly"]),
        ],
    )
    for repo, rule in zip(repos, rules, strict=True):
        for tag in ("infra-core", "haos"):
            restic_command(
                repo, password,
                ["forget", "--tag", tag, "--group-by", "tags",
                 *rule, "--prune"],
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
        backup_vm100(config, targets, password_file)
        save_result(config, targets)
        retention(config, targets, password_file)
    print("[ОК] Две шифрованные копии PVE, 910 и HAOS VM 100 записаны и проверены")


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
