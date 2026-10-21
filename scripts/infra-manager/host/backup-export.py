#!/usr/bin/env python3
"""Узкий PVE-only помощник: проверки носителей и поток защищённого снимка.

Не устанавливает пакеты, не размечает диски и не выполняет restic.
Полный поток выдаётся только локально по доверенному root SSH из управляющего гостя.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import secrets
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
INDEPENDENT_ACTIVATION = STATE / "operations/activation/runtime-activation-pending"
TRANSITION_ACTIVATION = STATE / "locks/runtime-activation-pending"
ACTIVE_910_OPERATION = STATE / "operations/active/910.json"
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
    if not expected or set(expected) - set(ALLOWED_MOUNTS):
        raise ExportError("Неожиданные или отсутствующие назначения резервирования")
    for name, value in expected.items():
        mount, fstype = ALLOWED_MOUNTS[name]
        if not isinstance(value, str) or not value or len(value) > 255:
            raise ExportError("Нет подтверждённого источника или UUID")
        _mounted(mount, fstype, value)
    if (len(expected) == 2
            and expected["fast-ssd"] == expected["nas-backup"]):
        raise ExportError("Оба архива не должны находиться на одном носителе")


def _assert_no_runtime_activation() -> None:
    """Блокировать копию при работе systemd даже после выхода Ansible.

    Активная запись UUID сохраняется на всём протяжении самообновления,
    а два маркера покрывают первое переключение со старого Docker.
    Проверка повторяется *под* backup-gate, чтобы закрыть гонку preflight.
    """
    for path in (
        INDEPENDENT_ACTIVATION,
        TRANSITION_ACTIVATION,
        ACTIVE_910_OPERATION,
    ):
        if path.is_symlink() or path.exists():
            raise ExportError(
                "Самообновление infra-manager ещё выполняется или требует проверки"
            )


def check_sources() -> int:
    if not Path("/etc/pve").is_mount():
        raise ExportError("PVE /etc/pve не подключён")
    if not STATE.is_dir() or STATE.is_symlink():
        raise ExportError("Постоянное состояние infra-manager недоступно")
    for path in SOURCES:
        if path.is_symlink() or not path.exists():
            raise ExportError("Отсутствует обязательный источник резервирования")
    for label, path in (
        ("unseal OpenBao", ROOT / "pve-only/openbao/unseal.key"),
        ("аварийный GitHub-ключ", ROOT / "pve-only/recovery/github_proxmox_repo_ed25519"),
        ("AppRole снимка OpenBao", ROLE),
        ("помощник Raft-снимка", RAFT_HELPER),
        ("идентичность PVE-гостя 910", PVE_IDENTITY),
    ):
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            # Только статическая метка компонента: не раскрывать значения и пути.
            raise ExportError(f"Не подготовлен PVE-only компонент: {label}")
    _assert_no_runtime_activation()
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
        # Проверить именно после успешного захвата эксклюзивного flock:
        # между preflight и этой точкой могла начаться новая активация.
        _assert_no_runtime_activation()
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


def stream_backup() -> None:
    vmid = check_sources()
    with _consistent_window():
        with tempfile.TemporaryDirectory(prefix="infra-snapshot-", dir="/run") as tmp:
            stage = Path(tmp)
            stage.chmod(0o700)
            _snapshot_openbao(vmid, stage / "openbao-raft.snap")
            manifest = {
                "schema_version": 1,
                "vmid": vmid,
                "raft_snapshot": True,
            }
            (stage / "manifest.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8",
            )
            # Закрытые исходники не сохраняются на PVE SSD, кроме штатного состояния.
            command = [
                "tar", "--create", "--file", "-",
                "--warning=no-file-changed",
                "--exclude=mnt/bindmounts/infra-manager/state/openbao",
                "--exclude=mnt/bindmounts/infra-manager/state/opentofu/provider-mirror",
                "--exclude=mnt/bindmounts/infra-manager/state/opentofu/.terraform",
                "-C", "/",
                *[str(path).lstrip("/") for path in SOURCES],
                "-C", str(stage), "manifest.json", "openbao-raft.snap",
            ]
            run = subprocess.run(command, stdout=sys.stdout.buffer,
                                 stderr=subprocess.DEVNULL, check=False)
            if run.returncode:
                raise ExportError("Не удалось собрать согласованный архив PVE")


def repositories_state(expected: dict[str, str]) -> dict[str, str]:
    """Проверить только подтверждённые назначения, не трогая другие."""
    result = {}
    for name in expected:
        mount, _fs = ALLOWED_MOUNTS[name]
        repo = mount / ("infra-core" if name == "fast-ssd" else "pve-infra-core")
        if repo.is_symlink():
            raise ExportError("Символьная ссылка вместо хранилища")
        if not repo.exists():
            result[name] = "missing"
        elif repo.is_dir() and not any(repo.iterdir()):
            result[name] = "missing"
        elif repo.is_dir() and not (repo / "config").is_symlink() and (repo / "config").is_file():
            result[name] = "existing"
        else:
            raise ExportError("В целевом каталоге есть неизвестные данные")
    return result


def password_bytes() -> bytes:
    """Выдать или создать один корневой ключ только после подтверждения носителей."""
    directory = Path("/etc/infra-manager/backup")
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ExportError("Небезопасный каталог ключа резервирования")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    password = directory / "restic-password"
    if not password.exists() and not password.is_symlink():
        try:
            descriptor = os.open(
                password, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write((secrets.token_urlsafe(48) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
    if (password.is_symlink() or not password.is_file()
            or password.stat().st_mode & 0o077
            or not 32 <= password.stat().st_size <= 256):
        raise ExportError("Не подтверждён защищённый ключ restic")
    return password.read_bytes()


def stream_vm100() -> None:
    """Без промежуточного файла PVE передать штатный снимок VM 100."""
    check = subprocess.run(
        ["qm", "status", "100"], capture_output=True,
        text=True, check=False, timeout=30,
    )
    if check.returncode:
        raise ExportError("Гость VM 100 недоступен")
    backup = subprocess.run(
        ["vzdump", "100", "--mode", "snapshot", "--compress", "zstd",
         # При --stdout Proxmox всё равно может требовать backup-content
         # для storage=local. Явный dumpdir отменяет выбор storage=local.
         # Содержимое архива по-прежнему идёт только в stdout, без файла на PVE.
         "--dumpdir", "/run", "--stdout", "1"],
        stdout=sys.stdout.buffer,
        stderr=subprocess.DEVNULL,
        check=False, timeout=7200,
    )
    if backup.returncode:
        raise ExportError("Потоковый снимок VM 100 не завершился")



# Резервирование LVM-томов: только объекты pve/data, известные по каталогу.
# Все источники дополнительно сверяются с фактическим PVE, а не выбираются
# из произвольного пути, присланного по SSH.
def _volume_contract(value: object) -> tuple[str, Path, Path, set[int]]:
    import re
    if not isinstance(value, dict) or set(value) != {"id", "mountpoint", "consumers"}:
        raise ExportError("Неверное описание тома резервирования")
    name = value["id"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
        raise ExportError("Недопустимое имя тома")
    mountpoint = value["mountpoint"]
    if (not isinstance(mountpoint, str)
            or not mountpoint.startswith("/mnt/bindmounts/")
            or ".." in Path(mountpoint).parts
            or "//" in mountpoint
            or str(Path(mountpoint)) != mountpoint):
        raise ExportError("Недопустимый путь тома резервирования")
    path = Path(mountpoint)
    if path.is_symlink() or not path.is_mount():
        raise ExportError("Том не подтверждён по точке подключения")
    device = Path("/dev/pve") / name
    if not device.exists() or not device.is_block_device():
        raise ExportError("Рабочий LVM-том не найден")
    check = subprocess.run(
        ["lvs", "--noheadings", "-o", "pool_lv", str(device)],
        text=True, capture_output=True, check=False, timeout=20,
    )
    if check.returncode or check.stdout.strip() != "data":
        raise ExportError("Том не принадлежит pve/data")
    actual = subprocess.run(
        ["findmnt", "-rn", "-o", "SOURCE,FSTYPE", "--mountpoint", str(path)],
        text=True, capture_output=True, check=False, timeout=20,
    )
    parts = actual.stdout.strip().split()
    if (actual.returncode or len(parts) != 2 or parts[1] != "ext4"
            or os.path.realpath(parts[0]) != os.path.realpath(device)):
        raise ExportError("Источник файловой системы не соответствует LVM")
    consumers = value["consumers"]
    if (not isinstance(consumers, list) or len(consumers) > 64
            or any(type(x) is not int or not 100 <= x <= 9999 for x in consumers)
            or len(consumers) != len(set(consumers))):
        raise ExportError("Некорректный перечень гостей тома")
    return name, path, device, set(consumers)


def _consumers_on_host(root: Path, declared: set[int]) -> list[int]:
    import re
    listed = subprocess.run(
        ["pct", "list"], text=True, capture_output=True, check=False, timeout=20,
    )
    if listed.returncode:
        raise ExportError("Не удалось получить перечень LXC")
    actual = []
    for line in listed.stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s", line)
        if not match:
            continue
        vmid = int(match.group(1))
        config = subprocess.run(
            ["pct", "config", str(vmid)], text=True,
            capture_output=True, check=False, timeout=20,
        )
        if config.returncode:
            raise ExportError("Не удалось проверить подключения LXC")
        for mount in re.findall(r"^mp\d+:\s*(/[^,\n]+)", config.stdout, re.M):
            if mount == str(root) or mount.startswith(str(root) + "/"):
                if vmid not in declared:
                    raise ExportError("Неизвестный потребитель резервируемого тома")
                actual.append(vmid)
                break
    return sorted(actual)


def _thin_pool_ready() -> None:
    result = subprocess.run(
        ["lvs", "--noheadings", "--nosuffix", "-o",
         "data_percent,metadata_percent", "--separator", ",", "pve/data"],
        text=True, capture_output=True, check=False, timeout=20,
    )
    if result.returncode:
        raise ExportError("Не удалось проверить свободное место LVM")
    try:
        used, meta = (float(x.strip()) for x in result.stdout.strip().split(","))
    except (ValueError, TypeError) as exc:
        raise ExportError("Нет достоверных данных о LVM thin-pool") from exc
    if not (0 <= used < 85 and 0 <= meta < 85):
        raise ExportError("LVM thin-pool близок к заполнению, снимок запрещён")


def volume_repositories(expected: dict[str, str], volume: dict) -> dict[str, str]:
    name, _root, _device, _declared = _volume_contract(volume)
    result = {}
    for destination in expected:
        mount, _fs = ALLOWED_MOUNTS[destination]
        base = mount / "volumes"
        target = base / name
        if base.is_symlink() or target.is_symlink():
            raise ExportError("Ссылка в пути Restic запрещена")
        if not target.exists() or (target.is_dir() and not any(target.iterdir())):
            result[destination] = "missing"
        elif target.is_dir() and (target / "config").is_file() and not (target / "config").is_symlink():
            result[destination] = "existing"
        else:
            raise ExportError("Неизвестные данные вместо репозитория тома")
    return result


def prepare_volume_repositories(expected: dict[str, str], volume: dict) -> None:
    """Создать только родительский каталог volumes на подтверждённом носителе."""
    name, _root, _device, _consumers = _volume_contract(volume)
    for destination in expected:
        mount, _fs = ALLOWED_MOUNTS[destination]
        base = mount / "volumes"
        target = base / name
        if base.is_symlink() or target.is_symlink():
            raise ExportError("Недоверенный путь для Restic")
        base.mkdir(mode=0o700, exist_ok=True)
        if not base.is_dir():
            raise ExportError("Не удалось подготовить каталог Restic")


def stream_volume(volume: dict) -> None:
    """Согласовать все LXC; снять тонкий snapshot; возобновить LXC до чтения."""
    import secrets
    name, root, device, declared = _volume_contract(volume)
    _thin_pool_ready()
    consumers = _consumers_on_host(root, declared)
    with _consistent_window():
        with tempfile.TemporaryDirectory(prefix="infra-volume-", dir="/run") as tmp:
            stage = Path(tmp)
            stage.chmod(0o700)
            target = stage / "data"
            target.mkdir(mode=0o700)
            snapshot = Path("/dev/pve") / ("imbackup_" + name + "_" + secrets.token_hex(5))
            created = False
            mounted = False
            stopped: list[int] = []
            try:
                try:
                    for vmid in consumers:
                        status = subprocess.run(
                            ["pct", "status", str(vmid)], text=True,
                            capture_output=True, check=False, timeout=20,
                        )
                        if status.returncode:
                            raise ExportError("Не удалось проверить состояние гостя")
                        if status.stdout.strip() == "status: running":
                            result = subprocess.run(
                                ["pct", "shutdown", str(vmid), "--timeout", "40"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False, timeout=60,
                            )
                            if result.returncode:
                                raise ExportError("Не удалось согласованно остановить гостя")
                            stopped.append(vmid)
                        elif status.stdout.strip() != "status: stopped":
                            raise ExportError("Неизвестное состояние LXC")
                    _thin_pool_ready()
                    result = subprocess.run(
                        ["lvcreate", "--snapshot", "--name", snapshot.name, str(device)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False, timeout=60,
                    )
                    if result.returncode:
                        raise ExportError("Создание временного LVM snapshot не удалось")
                    created = True
                finally:
                    failed = False
                    for vmid in reversed(stopped):
                        resumed = subprocess.run(
                            ["pct", "start", str(vmid)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, timeout=90,
                        )
                        failed = failed or resumed.returncode != 0
                    if failed:
                        raise ExportError("После снимка не удалось восстановить работу LXC")
                result = subprocess.run(
                    ["mount", "-t", "ext4", "-o", "ro,noload", str(snapshot), str(target)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=40,
                )
                if result.returncode:
                    raise ExportError("Снимок тома не удалось подключить только для чтения")
                mounted = True
                result = subprocess.run(
                    ["tar", "--create", "--file", "-", "--numeric-owner",
                     "--xattrs", "--acls", "--one-file-system", "-C", str(target), "."],
                    stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL,
                    check=False, timeout=7200,
                )
                if result.returncode:
                    raise ExportError("Поток резервируемого тома оборвался")
            finally:
                cleanup_error = None
                if mounted:
                    unmount = subprocess.run(
                        ["umount", str(target)], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False, timeout=90,
                    )
                    if unmount.returncode:
                        cleanup_error = "Не удалось отключить временный снимок"
                if created and cleanup_error is None:
                    removed = subprocess.run(
                        ["lvremove", "--yes", "--force", str(snapshot)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False, timeout=120,
                    )
                    if removed.returncode:
                        cleanup_error = "Не удалось удалить временный LVM snapshot"
                if cleanup_error:
                    raise ExportError(cleanup_error)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "export", "password", "password-existing", "repositories", "vm100", "volume-repositories", "volume-prepare", "volume"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Нужны полномочия PVE-only root")
    try:
        expected = json.load(sys.stdin)
        if not isinstance(expected, dict):
            raise ExportError("Неверное описание носителей")
        volume = None
        if args.command in ("volume-repositories", "volume-prepare", "volume"):
            if set(expected) != {"destinations", "volume"}:
                raise ExportError("Неверное описание задания тома")
            volume = expected["volume"]
            expected = expected["destinations"]
        check_destinations(expected)
        check_sources()
        if args.command == "password":
            sys.stdout.buffer.write(password_bytes())
        elif args.command == "password-existing":
            existing = Path("/etc/infra-manager/backup/restic-password")
            if not existing.is_file() or existing.is_symlink():
                raise ExportError("Ключ резервирования ещё не создан")
            sys.stdout.buffer.write(password_bytes())
        elif args.command == "check":
            print("[ОК] Источники и физические носители подтверждены")
        elif args.command == "repositories":
            print(json.dumps(repositories_state(expected)))
        elif args.command == "vm100":
            stream_vm100()
        elif args.command == "volume-repositories":
            print(json.dumps(volume_repositories(expected, volume)))
        elif args.command == "volume-prepare":
            prepare_volume_repositories(expected, volume)
        elif args.command == "volume":
            stream_volume(volume)
        else:
            stream_backup()
        return 0
    except (ExportError, OSError, ValueError, KeyError) as exc:
        # Во время двоичного экспорта stdout зарезервирован исключительно для tar.
        print(f"[ОШИБКА] Архив не выдан: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
