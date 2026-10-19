"""Зашифрованное резервирование под управлением 910, без restic на PVE.

Поток PVE-only читается из ограниченного помощника; restic работает в 910
и пишет через штатный SFTP сервер PVE в подтверждённые назначения.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .common import InfraManagerError
from .managed_backup import CONFIG, HELPER, EXPECTED_MOUNTS, _targets
from .operation_store import OperationStore, _atomic_json
from .pve_host import _ssh_command
from .settings import PATHS

REPOS = {"fast-ssd": "infra-core", "nas-backup": "pve-infra-core"}


def _remote(node: str, action: str, targets: dict[str, str]) -> bytes:
    """Служебные текстовые ответы без вывода закрытых данных."""
    result = subprocess.run(
        _ssh_command(node, ("python3", HELPER, action)),
        input=json.dumps(targets).encode(),
        capture_output=True, timeout=180, check=False,
    )
    if result.returncode or len(result.stdout) > 8192:
        raise InfraManagerError("PVE не подтвердил безопасное резервирование")
    return result.stdout


def _restic_env(node: str, password_path: Path, repo: Path) -> tuple[list[str], dict[str, str]]:
    # Пароль хранится в закрытом временном файле, не попадает в argv или лог.
    ssh = _ssh_command(node, ("true",))
    # _ssh_command уже проверяет известный хост и ключ; удаляем лишь
    # последнюю часть команды для вызова SFTP через restic.
    command = ssh[:-2] + [f"root@{node}", "-s", "sftp"]
    base = ["restic", "-o", "sftp.command=" + " ".join(
        __import__("shlex").quote(item) for item in command
    ), "-r", f"sftp:root@{node}:{repo}"]
    env = os.environ.copy()
    env["RESTIC_PASSWORD_FILE"] = str(password_path)
    return base, env


def _restic(base: list[str], env: dict[str, str], arguments: list[str]) -> None:
    completed = subprocess.run(
        base + arguments, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=7200, check=False,
    )
    if completed.returncode:
        raise InfraManagerError("restic не подтвердил операцию над зашифрованным архивом")


def _stream_to_repository(
    node: str, targets: dict[str, str], base: list[str], env: dict[str, str],
    action: str, filename: str,
) -> None:
    """Прервать запись при ошибке как источника, так и приёмника."""
    pve = subprocess.Popen(
        _ssh_command(node, ("python3", HELPER, action)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert pve.stdin is not None and pve.stdout is not None
        pve.stdin.write(json.dumps(targets).encode())
        pve.stdin.close()
        archive = subprocess.run(
            base + ["backup", "--stdin", "--stdin-filename", filename],
            stdin=pve.stdout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, timeout=7200, check=False,
        )
        pve.stdout.close()
        source_exit = pve.wait(timeout=60)
        if source_exit != 0 or archive.returncode != 0:
            raise InfraManagerError("Архив не подтверждён: ошибка источника или restic")
    finally:
        if pve.poll() is None:
            pve.kill()
            pve.wait()


def run_backup(node: str, config_path: Path = CONFIG) -> dict:
    """Сделать две независимые проверенные копии без потери старых."""
    import shutil
    import yaml

    if shutil.which("restic") is None:
        raise InfraManagerError("В управляющем госте не установлен restic")
    if config_path.is_symlink() or not config_path.is_file():
        raise InfraManagerError("Нет защищённого backup.yaml")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("host") != "pve":
        raise InfraManagerError("Неверное описание резервирования")
    targets = _targets(config)
    if not node or any(ch.isspace() for ch in node):
        raise InfraManagerError("Неверный адрес PVE")
    # Никаких записей до проверки двух физических томов на PVE.
    if _remote(node, "check", targets).strip() != (
        "[ОК] Источники и физические носители подтверждены".encode()
    ):
        raise InfraManagerError("Носители не подтверждены")
    existing = json.loads(_remote(node, "repositories", targets))
    if set(existing) != set(REPOS) or not set(existing.values()) <= {"existing", "missing"}:
        raise InfraManagerError("Неподтверждённые хранилища restic")
    store = OperationStore()
    # Запуск только при отсутствии выполняемой активации; независимо
    # от браузера и контейнера runtime.
    if list(store.active.glob("*.json")):
        raise InfraManagerError("Есть незавершённые инфраструктурные задания")
    with tempfile.TemporaryDirectory(prefix="infra-backup-", dir="/run") as temporary:
        password = Path(temporary) / "restic-password"
        secret = _remote(node, "password", targets)
        if not 32 <= len(secret) <= 256:
            raise InfraManagerError("Неверный закрытый ключ резервирования")
        password.write_bytes(secret)
        password.chmod(0o600)
        for label, name in REPOS.items():
            location = Path(EXPECTED_MOUNTS[label]) / name
            base, env = _restic_env(node, password, location)
            if existing[label] == "missing":
                _restic(base, env, ["init"])
            _stream_to_repository(
                node, targets, base, env, "export", "pve-manager.tar"
            )
            _stream_to_repository(
                node, targets, base, env, "vm100", "vm100.vma.zst"
            )
            _restic(base, env, ["check", "--read-data"])
        result = {
            "schema_version": 1,
            "created_utc": datetime.now(UTC).isoformat(),
            "destinations": ["fast-ssd", "nas-backup"],
            "includes_vm100": True,
            "restore_tested": False,
        }
        _atomic_json(store.settings / "backup-last-full.json", result)
        return result
