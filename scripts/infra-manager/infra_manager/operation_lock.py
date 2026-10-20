"""Блокировка штатных операций над гостями infra-manager."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .common import InfraManagerError
from .settings import PATHS


TRUSTED_STATE_LOCKS = Path("/mnt/persistent-state/locks")
DEFAULT_RUNTIME_LOCKS = Path("/var/lib/infra-manager/locks")


def _is_mount(path: Path) -> bool:
    """Проверить bind mount даже для двух каталогов одной файловой системы."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as entries:
            return any(
                len(fields) > 4 and fields[4] == str(path)
                for line in entries
                if (fields := line.split(" - ", 1)[0].split())
            )
    except OSError as exc:
        raise InfraManagerError(
            "Не удалось подтвердить постоянное подключение каталога блокировок"
        ) from exc


def _lock_root() -> Path:
    """Использовать только общий state/locks, никогда не данные Semaphore.

    При потере bind mount новый локальный каталог создавать нельзя:
    разные исполнители иначе получат независимые блокировки и резервирование
    может совпасть с изменяющей инфраструктурной операцией.
    """

    root = PATHS.data_dir / "locks"
    # В LXC 910 Ansible создаёт ссылку именно на persistent-state/locks.
    # В Docker тот же каталог подключён напрямую через bind mount.
    # Разрешаем только известную ссылку, не произвольное перенаправление.
    if root.is_symlink():
        try:
            resolved = root.resolve(strict=True)
            expected = TRUSTED_STATE_LOCKS.resolve(strict=True)
        except OSError as exc:
            raise InfraManagerError(
                "Общий каталог state/locks недоступен"
            ) from exc
        if resolved != expected or not _is_mount(TRUSTED_STATE_LOCKS.parent):
            raise InfraManagerError(
                "Каталог state/locks не связан с подтверждённым persistent-state"
            )
    elif root == DEFAULT_RUNTIME_LOCKS and not _is_mount(root):
        raise InfraManagerError(
            "Docker bind mount state/locks отсутствует; операция остановлена"
        )
    if not root.is_dir() or not os.access(root, os.W_OK):
        raise InfraManagerError(
            "Общий каталог блокировок state/locks не подключён или недоступен; "
            "изменяющая операция остановлена без использования Semaphore"
        )
    return root


def _lock_path(vmid: int, lock_dir: Path | None = None) -> Path:
    """Получить отдельный путь блокировки для выбранного VMID."""
    root = lock_dir if lock_dir is not None else _lock_root()
    return root / f"{vmid}.lock"


def _read_lock_owner(descriptor: int) -> str:
    """Прочитать описание владельца блокировки для понятной диагностики.

    Отсутствие или повреждение описания не даёт права обходить
    саму файловую блокировку."""
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 4096).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(data, dict):
        return raw

    operation = data.get("operation")
    pid = data.get("pid")
    started_at = data.get("started_at")
    parts = []
    if operation:
        parts.append(f"операция {operation}")
    if pid:
        parts.append(f"PID {pid}")
    if started_at:
        parts.append(f"с {started_at}")
    return ", ".join(parts)


@contextmanager
def project_checkout_lock(*, exclusive: bool) -> Iterator[None]:
    """Защитить общую рабочую копию проекта от одновременного изменения."""

    path = _lock_root() / "project.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    try:
        os.fchmod(descriptor, 0o660)
    except PermissionError:
        pass
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InfraManagerError(
                "Рабочая копия проекта занята другой операцией"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def _backup_gate(lock_dir: Path | None = None) -> Iterator[None]:
    """Не допускать изменяющие операции одновременно с копией PVE.

    Менеджерские задания держат общую разделяемую блокировку, а физический
    PVE требует исключительную для согласованной копии OpenTofu и секретов.
    """

    root = lock_dir if lock_dir is not None else _lock_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "backup-gate.lock"
    descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InfraManagerError(
                "Идёт резервирование PVE; изменяющие операции временно запрещены"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def guest_operation_lock(
    vmid: int,
    operation: str,
    *,
    lock_dir: Path | None = None,
) -> Iterator[None]:
    """Запретить параллельные изменяющие операции над одним VMID."""

    with _backup_gate(lock_dir):
        path = _lock_path(vmid, lock_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
    
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
        try:
            os.fchmod(descriptor, 0o660)
        except PermissionError:
            pass
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner = _read_lock_owner(descriptor)
                suffix = f" ({owner})" if owner else ""
                raise InfraManagerError(
                    f"Для гостя {vmid} уже выполняется другая операция{suffix}"
                ) from exc
    
            payload = {
                "vmid": vmid,
                "operation": operation,
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
    
            try:
                yield
            finally:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
