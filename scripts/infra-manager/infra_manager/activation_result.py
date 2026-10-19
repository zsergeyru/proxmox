"""Подтверждённые результаты активации; не зависят от веб-панели и планировщика.

Системная служба 910 записывает результат в защищённое постоянное состояние.
Оператор ждёт файл по уникальному идентификатору, поэтому не зависит от
момента удаления временного маркера и не принимает результат другого запуска.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import InfraManagerError

RESULT_DIR = Path("/mnt/persistent-state/operations/runtime-activation")
STATE_MOUNT = Path("/mnt/persistent-state")
_ID = re.compile(r"[0-9a-f]{32}\Z")


def _validate_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise InfraManagerError("Некорректный идентификатор активации")
    return value


def _check_directory(directory: Path, *, create: bool) -> None:
    """Не писать результаты в ссылку или общедоступный каталог."""
    if create:
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
    for path in (directory.parent, directory):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise InfraManagerError(f"Недоверенный каталог результатов: {path}")
        if info.st_mode & 0o077:
            raise InfraManagerError(f"Слишком широкие права каталога: {path}")


def _write_atomically(destination: Path, data: dict) -> None:
    """Защищённая запись с атомарной публикацией и без временных имён PID."""
    descriptor, name = tempfile.mkstemp(prefix=".activation-", dir=destination.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def record_result(
    identifier: str,
    status: str,
    marker: str,
    *,
    directory: Path = RESULT_DIR,
) -> str:
    """Записать результат под отдельным именем и обновить указатель последнего.

    Для переходного вызова без идентификатора разрешён маркер 'legacy'.
    Ранее записанный результат другого задания при этом не уничтожается.
    """
    if identifier == "legacy":
        identifier = uuid.uuid4().hex
    _validate_id(identifier)
    if status not in {"ok", "failed"}:
        raise InfraManagerError("Недопустимый результат активации")
    if len(marker) > 160 or "\n" in marker or "\r" in marker:
        raise InfraManagerError("Некорректная отметка активации")
    _check_directory(directory, create=True)
    payload = {
        "schema_version": 1,
        "id": identifier,
        "status": status,
        "marker": marker,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_atomically(directory / f"{identifier}.json", payload)
    _write_atomically(directory / "latest.json", payload)
    return identifier


def read_result(identifier: str, *, directory: Path = RESULT_DIR) -> dict | None:
    """Не следовать символьным ссылкам и не принимать чужой результат."""
    identifier = _validate_id(identifier)
    path = directory / f"{identifier}.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InfraManagerError("Не удалось прочитать результат активации") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError) as exc:
        raise InfraManagerError("Повреждён результат активации") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("id") != identifier
        or data.get("status") not in {"ok", "failed"}
    ):
        raise InfraManagerError("Некорректное содержимое результата активации")
    return data


def await_result(
    identifier: str,
    *,
    directory: Path = RESULT_DIR,
    timeout_seconds: float = 1080,
    interval_seconds: float = 1,
) -> dict:
    """Дождаться результата своего задания независимо от временного маркера."""
    identifier = _validate_id(identifier)
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = read_result(identifier, directory=directory)
        if result is not None:
            if result["status"] != "ok":
                raise InfraManagerError(
                    "Отложенная активация управляющей среды завершилась ошибкой; "
                    "проверьте /var/log/infra-manager/runtime-activation.log"
                )
            return result
        if time.monotonic() >= deadline:
            raise InfraManagerError(
                "Не получено подтверждение активации управляющей среды; "
                "проверьте /var/log/infra-manager/runtime-activation.log"
            )
        time.sleep(interval_seconds)


def main() -> int:
    """Внутренний вызов только из службы systemd, не операторская команда."""
    if len(sys.argv) != 5 or sys.argv[1] != "record":
        raise InfraManagerError("Некорректный вызов записи результата")
    if os.geteuid() != 0 or not os.path.ismount(STATE_MOUNT):
        raise InfraManagerError("Нет прав root или постоянного подключения состояния")
    identifier = record_result(sys.argv[2], sys.argv[3], sys.argv[4])
    print(f"[ИНФО] Результат активации сохранён: {identifier}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InfraManagerError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
