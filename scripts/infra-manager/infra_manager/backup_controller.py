"""Расписание и зашифрованные копии под управлением управляющего LXC.

PVE выполняет только проверку подключений и выдачу защищённого снимка.
restic и запись через SFTP выполняются исключительно внутри 910.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .common import InfraManagerError
from .pve_host import _ssh_command
from .operation_store import OperationStore, _atomic_json
from .settings import PATHS

POLICY = PATHS.repo_root / "infrastructure/hosts/pve/backup.yaml"
HOST_HELPER = "/usr/local/lib/infra-manager/backup/backup-export.py"
DESTINATIONS = {
    "fast-ssd": ("/mnt/pve/backup-fast", "ext4", "infra-core"),
    "nas-backup": ("/mnt/pve/nas-backup", "nfs4", "pve-infra-core"),
}


def policy() -> tuple[dict, dict[str, str]]:
    """Загрузить только единое описание backup.yaml, не локальные настройки."""
    if POLICY.is_symlink() or not POLICY.is_file():
        raise InfraManagerError("Нет описания backup.yaml из рабочей копии проекта")
    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise InfraManagerError("Неверная версия backup.yaml")
    if raw.get("host") != "pve" or raw.get("job") != "infra-core":
        raise InfraManagerError("Неверный контур резервирования")
    destinations = raw.get("destinations")
    if not isinstance(destinations, list) or len(destinations) != 2:
        raise InfraManagerError("Нужны SSD и NAS для защищённых копий")
    verified = {}
    for dest in destinations:
        if not isinstance(dest, dict):
            raise InfraManagerError("Некорректное назначение резервирования")
        name = dest.get("id")
        if name not in DESTINATIONS or name in verified:
            raise InfraManagerError("Неизвестное или дублирующееся назначение")
        path, filesystem, repository = DESTINATIONS[name]
        if (dest.get("mountpoint"), dest.get("filesystem"),
                dest.get("repository")) != (path, filesystem, repository):
            raise InfraManagerError("Изменение защищённого назначения запрещено")
        identity = dest.get("uuid") or dest.get("source")
        if not isinstance(identity, str) or not identity:
            raise InfraManagerError(
                "Носители не подтверждены в backup.yaml; ожидание настройки SSD/NAS"
            )
        verified[name] = identity
    if set(verified) != set(DESTINATIONS):
        raise InfraManagerError("Нет одного из независимых носителей")
    return raw, verified


def _remote(node: str, action: str, payload: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Ограниченный вызов PVE helper без произвольного удалённого shell."""
    # Двоичный ответ (в том числе ключ в /run) не проходит через
    # текстовый CommandRunner, который предназначен для обычных заданий.
    process = subprocess.Popen(
        _ssh_command(node, ("python3", HOST_HELPER, action)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        output, _ = process.communicate(payload, timeout=90)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise InfraManagerError("PVE helper превысил время ответа") from exc
    if process.returncode:
        raise InfraManagerError(f"PVE не подтвердил действие резервирования: {action}")
    return subprocess.CompletedProcess(process.args, process.returncode, output)


def _source_node() -> str:
    """Получить имя PVE из уже доверенной операторской конфигурации."""
    from .operator import _pve_node

    node = _pve_node()
    if not node or any(ch.isspace() for ch in node):
        raise InfraManagerError("Не удалось определить узел PVE")
    return node


def _credentials(node: str, directory: Path, verified: dict[str, str],
                 *, create: bool) -> Path:
    """Получить ключ по доверенному SSH только после проверки двух носителей."""
    action = "password" if create else "password-existing"
    secret = _remote(node, action, json.dumps(verified).encode()).stdout
    if not 32 <= len(secret) <= 256 or b"\x00" in secret:
        raise InfraManagerError("Не получен безопасный пароль резервирования")
    name = directory / "restic-password"
    with name.open("xb") as stream:
        stream.write(secret)
    name.chmod(0o600)
    return name


def _restic(node: str, destination: str, key: Path, args: list[str],
            *, source=None) -> None:
    """Запустить restic в 910 через подтверждённый root SSH/SFTP к PVE."""
    mountpoint, _filesystem, repository = DESTINATIONS[destination]
    backend = f"sftp:root@{node}:{mountpoint}/{repository}"
    ssh_args = [
        "-i", str(PATHS.pve_host_private_key),
        "-o", f"UserKnownHostsFile={PATHS.pve_host_known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
    ]
    cmd = [
        "restic", "-o", f"sftp.args={shlex.join(ssh_args)}",
        "-r", backend, *args,
    ]
    env = dict(os.environ, RESTIC_PASSWORD_FILE=str(key))
    process = subprocess.Popen(
        cmd, stdin=source, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=env,
    )
    try:
        code = process.wait(timeout=7200)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise InfraManagerError("Операция restic превысила время выполнения") from exc
    if code:
        raise InfraManagerError(
            f"Проверка или запись restic в {destination} завершилась с ошибкой"
        )


def _stream_snapshot(node: str, verified: dict[str, str],
                     key: Path, destination: str, *, vm100: bool = False) -> None:
    """Передать последовательный полный поток PVE или VM 100 в restic 910."""
    payload = json.dumps(verified, separators=(",", ":")).encode()
    export = subprocess.Popen(
        _ssh_command(node, ("python3", HOST_HELPER,
                            "vm100" if vm100 else "export")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        assert export.stdin is not None
        export.stdin.write(payload)
        export.stdin.close()
        assert export.stdout is not None
        try:
            _restic(
                node, destination, key,
                ["backup", "--stdin", "--stdin-filename",
                 "haos-100.vma.zst" if vm100 else "infra-core.tar",
                 "--tag", "haos" if vm100 else "infra-core",
                 "--tag", "vm-100" if vm100 else "pve"],
                source=export.stdout,
            )
        finally:
            export.stdout.close()
        if export.wait(timeout=2500) != 0:
            raise InfraManagerError("PVE не подтвердил полный поток согласованного снимка")
    finally:
        if export.poll() is None:
            export.kill()
            export.wait()


def _vm100_due(store: OperationStore) -> bool:
    """Ежедневная копия HAOS не дублируется при каждом запуске таймера."""
    saved = store.settings / "backup-vm100.json"
    if saved.is_symlink() or not saved.is_file():
        return True
    try:
        record = json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (
        isinstance(record, dict)
        and record.get("date") == datetime.now(UTC).date().isoformat()
        and record.get("destinations") == list(DESTINATIONS)
        and record.get("verified") is True
    )


def execute(*, run: bool = False) -> None:
    """Без ключа или двух подтверждённых носителей не производить запись."""
    _description, verified = policy()
    if shutil.which("restic") is None:
        raise InfraManagerError("restic не установлен внутри управляющего гостя")
    node = _source_node()
    _remote(node, "check", json.dumps(verified).encode())
    with tempfile.TemporaryDirectory(prefix="infra-backup-", dir="/run") as path:
        directory = Path(path)
        directory.chmod(0o700)
        key = _credentials(node, directory, verified, create=run)
        state = json.loads(_remote(
            node, "repositories", json.dumps(verified).encode()
        ).stdout)
        if not isinstance(state, dict) or set(state) != set(DESTINATIONS):
            raise InfraManagerError("PVE не подтвердил состояние двух restic-хранилищ")
        # Сначала сверить все уже созданные копии и только потом
        # разрешить инициализацию действительно пустых каталогов.
        for destination in DESTINATIONS:
            if state[destination] == "existing":
                _restic(node, destination, key, ["snapshots", "--json"])
            elif state[destination] != "missing":
                raise InfraManagerError("На носителе неизвестное состояние хранилища")
        if not run:
            if any(value != "existing" for value in state.values()):
                raise InfraManagerError("Хранилища restic будут созданы только при запуске резервирования")
            print("[ОК] Подключения, ключ и оба хранилища restic подтверждены")
            return
        for destination in DESTINATIONS:
            if state[destination] == "missing":
                _restic(node, destination, key, ["init"])
            _restic(node, destination, key, ["snapshots", "--json"])
        for destination in DESTINATIONS:
            _stream_snapshot(node, verified, key, destination)
            _restic(node, destination, key, ["check", "--read-data-subset=1%"])
        store = OperationStore()
        created = datetime.now(UTC)
        _atomic_json(
            store.settings / "backup-last-core.json",
            {
                "schema_version": 1,
                "created_utc": created.isoformat(),
                "destinations": list(DESTINATIONS),
                "verified": "restic-check-read-data-subset-1-percent",
                "includes_vm100": False,
                "restore_tested": False,
            },
        )
        if _vm100_due(store):
            for destination in DESTINATIONS:
                _stream_snapshot(node, verified, key, destination, vm100=True)
                _restic(node, destination, key, ["check", "--read-data-subset=1%"])
            _atomic_json(store.settings / "backup-vm100.json", {
                "date": datetime.now(UTC).date().isoformat(),
                "destinations": list(DESTINATIONS),
                "verified": True,
            })
        _atomic_json(store.settings / "backup-last-full.json", {
            "schema_version": 1,
            "created_utc": created.isoformat(),
            "destinations": list(DESTINATIONS),
            "includes_vm100": True,
            "restore_tested": False,
        })
        print("[ОК] Состояние PVE/910 и ежедневный снимок VM 100 сохранены "
              "в обоих хранилищах; пробное восстановление ещё не подтверждено")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InfraManagerError("Нужны полномочия системной службы 910")
        execute(run=args.run)
        return 0
    except (InfraManagerError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"[ОЖИДАНИЕ] Резервирование: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
