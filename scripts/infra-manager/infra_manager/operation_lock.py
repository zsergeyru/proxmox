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


def _lock_path(vmid: int, lock_dir: Path | None = None) -> Path:
    root = lock_dir if lock_dir is not None else PATHS.data_dir / "locks"
    return root / f"{vmid}.lock"


def _read_lock_owner(descriptor: int) -> str:
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

    path = PATHS.data_dir / "locks" / "project.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    os.fchmod(descriptor, 0o660)
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
def guest_operation_lock(
    vmid: int,
    operation: str,
    *,
    lock_dir: Path | None = None,
) -> Iterator[None]:
    """Запретить параллельные изменяющие операции над одним VMID."""

    path = _lock_path(vmid, lock_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
    os.fchmod(descriptor, 0o660)
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
