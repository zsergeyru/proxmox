#!/usr/bin/env python3
"""Узкий PVE-only помощник: проверки носителей и поток защищённого снимка.

Не устанавливает пакеты, не размечает диски и не выполняет restic.
Полный поток выдаётся только локально по доверенному root SSH из 910.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path("/mnt/bindmounts/infra-manager")
STATE = ROOT / "state"
ALLOWED_MOUNTS = {
    "fast-ssd": (Path("/mnt/pve/backup-fast"), "ext4"),
    "nas-backup": (Path("/mnt/pve/nas-backup"), "nfs4"),
}
PVE_IDENTITY = Path("/etc/infra-manager/openbao-host.json")
ROLE = ROOT / "pve-only/openbao/backup-access.json"
RAFT_HELPER = Path("/usr/local/lib/infra-manager/backup/openbao-raft-guest.py")
BACKUP_GATE = STATE / "locks/backup-gate.lock"
ACTIVATION = STATE / "semaphore/.infra-manager-runtime-activation-pending"
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
    STATE / "operations",
)


class ExportError(RuntimeError):
    """Отказ защищённого помощника без выдачи закрытого содержимого."""


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ExportError("Защищённый файл состояния отсутствует")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ExportError("Неверная структура защищённого состояния")
    return obj


def _mounted(mount: Path, expected_fstype: str, expected_source: str) -> None:
    if mount.is_symlink() or not mount.is_mount():
        raise ExportError("Назначение не является точкой подключения")
    run = subprocess.run(
        ["findmnt", "--json", "-M", str(mount), "-o", "TARGET,SOURCE,FSTYPE,UUID"],
        capture_output=True, text=True, check=False,
    )
    if run.returncode:
        raise ExportError("Не удалось проверить носитель")
    records = json.loads(run.stdout).get("filesystems", [])
    if len(records) != 1 or records[0].get("target") != str(mount):
        raise ExportError("Точка подключения не подтверждена")
    entry = records[0]
    if entry.get("fstype") != expected_fstype:
        raise ExportError("Файловая система назначения не соответствует описанию")
    source = str(entry.get("source") or "")
    uuid = str(entry.get("uuid") or "")
    if not expected_source or expected_source not in {source, uuid}:
        raise ExportError("Идентичность носителя не подтверждена")


def check_destinations(expected: dict[str, str]) -> None:
    if set(expected) != set(ALLOWED_MOUNTS):
        raise ExportError("Неожиданные назначения резервирования")
    for name, (mount, fstype) in ALLOWED_MOUNTS.items():
        value = expected.get(name)
        if not isinstance(value, str) or not value or len(value) > 255:
            raise ExportError("Нет подтверждённого источника или UUID")
        _mounted(mount, fstype, value)
    if expected["fast-ssd"] == expected["nas-backup"]:
        raise ExportError("Оба архива не должны находиться на одном носителе")


def check_sources() -> int:
    if not Path("/etc/pve").is_mount():
        raise ExportError("PVE /etc/pve не подключён")
    if not STATE.is_dir() or STATE.is_symlink():
        raise ExportError("Постоянное состояние infra-manager недоступно")
    for path in SOURCES:
        if path.is_symlink() or not path.exists():
            raise ExportError("Отсутствует обязательный источник резервирования")
    for path in (
        ROOT / "pve-only/openbao/unseal.key",
        ROOT / "pve-only/recovery/github_proxmox_repo_ed25519",
        ROLE, RAFT_HELPER, PVE_IDENTITY,
    ):
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ExportError("Неполное PVE-only состояние для резервирования")
    if ACTIVATION.exists() or ACTIVATION.is_symlink():
        raise ExportError("Самообновление infra-manager ещё выполняется")
    role = _read_json(ROLE)
    if set(role) != {"role_id", "secret_id"} or not all(
        isinstance(item, str) and item for item in role.values()
    ):
        raise ExportError("Не подготовлен ограниченный AppRole снимка OpenBao")
    identity = _read_json(PVE_IDENTITY)
    vmid = identity.get("vmid")
    if not isinstance(vmid, int) or isinstance(vmid, bool) or vmid <= 0:
        raise ExportError("Не подтверждён управляющий LXC")
    check = subprocess.run(
        ["pct", "config", str(vmid)],
        capture_output=True, text=True, check=False,
    )
    if check.returncode or not all(
        value in check.stdout
        for value in ("hostname: infra-manager", "unprivileged: 1",
                      "owner=proxmox-project", "role=infra-manager")
    ):
        raise ExportError("Подменён управляющий LXC")
    return vmid


@contextmanager
def _consistent_window():
    if BACKUP_GATE.is_symlink() or not BACKUP_GATE.is_file():
        raise ExportError("Нет общей блокировки изменений")
    handle = os.open(BACKUP_GATE, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExportError("Выполняются изменения инфраструктуры") from exc
        yield
    finally:
        os.close(handle)


def _snapshot_openbao(vmid: int, target: Path) -> None:
    role = _read_json(ROLE)
    code = RAFT_HELPER.read_text(encoding="utf-8")
    with target.open("xb") as output:
        completed = subprocess.run(
            ["pct", "exec", str(vmid), "--", "python3", "-c", code],
            input=json.dumps(role), text=True, stdout=output,
            stderr=subprocess.DEVNULL, timeout=240, check=False,
        )
    if completed.returncode or not 256 <= target.stat().st_size <= 2 * 1024**3:
        raise ExportError("Согласованный снимок OpenBao Raft не получен")


def _optional_sqlite(target: Path) -> bool:
    """При переходе сохранить SQLite только если старая база ещё существует."""
    source = STATE / "semaphore/semaphore.sqlite"
    if source.is_symlink() or not source.is_file():
        return False
    uri = f"file:{source}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        with sqlite3.connect(target) as copy:
            db.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ExportError("Переходная база Semaphore повреждена")
    return True


def stream_backup() -> None:
    vmid = check_sources()
    with _consistent_window():
        with tempfile.TemporaryDirectory(prefix="infra-snapshot-", dir="/run") as tmp:
            stage = Path(tmp)
            stage.chmod(0o700)
            _snapshot_openbao(vmid, stage / "openbao-raft.snap")
            legacy = _optional_sqlite(stage / "semaphore-legacy.sqlite")
            manifest = {
                "schema_version": 1,
                "vmid": vmid,
                "raft_snapshot": True,
                "legacy_sqlite": legacy,
            }
            (stage / "manifest.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8",
            )
            # Закрытые исходники не сохраняются на PVE SSD, кроме штатного состояния.
            command = [
                "tar", "--create", "--file", "-",
                "--warning=no-file-changed",
                "--exclude=mnt/bindmounts/infra-manager/state/openbao",
                "--exclude=mnt/bindmounts/infra-manager/state/semaphore",
                "--exclude=mnt/bindmounts/infra-manager/state/opentofu/provider-mirror",
                "--exclude=mnt/bindmounts/infra-manager/state/opentofu/.terraform",
                "-C", "/",
                *[str(path).lstrip("/") for path in SOURCES],
                "-C", str(stage), "manifest.json", "openbao-raft.snap",
            ]
            if legacy:
                command.append("semaphore-legacy.sqlite")
            run = subprocess.run(command, stdout=sys.stdout.buffer,
                                 stderr=subprocess.DEVNULL, check=False)
            if run.returncode:
                raise ExportError("Не удалось собрать согласованный архив PVE")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "export", "password"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Нужны полномочия PVE-only root")
    try:
        if args.command == "password":
            password = Path("/etc/infra-manager/backup/restic-password")
            if (password.is_symlink() or not password.is_file()
                    or password.stat().st_mode & 0o077
                    or not 32 <= password.stat().st_size <= 256):
                raise ExportError("Не подтверждён защищённый ключ restic")
            with password.open("rb") as secret:
                sys.stdout.buffer.write(secret.read(256))
        else:
            expected = json.load(sys.stdin)
            if not isinstance(expected, dict):
                raise ExportError("Неверное описание носителей")
            check_destinations(expected)
            if args.command == "check":
                check_sources()
                print("[ОК] Источники и физические носители подтверждены")
            else:
                stream_backup()
        return 0
    except (ExportError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        # Во время двоичного экспорта stdout зарезервирован исключительно для tar.
        print(f"[ОШИБКА] Архив не выдан: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
