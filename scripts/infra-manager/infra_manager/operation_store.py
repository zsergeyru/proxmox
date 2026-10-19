"""Постоянный учёт инфраструктурных операций вне Docker и Semaphore.

Запись выполняется только в подтверждённый каталог состояния LXC 910.
Незавершённые задания не возобновляются автоматически после отказа.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from .common import InfraManagerError

DEFAULT_ROOT = Path("/mnt/persistent-state/operations")
MOUNT_ROOT = Path("/mnt/persistent-state")
KINDS = frozenset({"deploy", "sync", "repair", "status"})
TERMINAL = frozenset({"completed", "failed", "needs-recovery"})
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_RECORD_BYTES = 65536


def _now() -> str:
    """Вернуть время в UTC для записи операции."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _mounted(path: Path) -> bool:
    """Проверить именно точку монтирования, а не обычный каталог rootfs."""
    target = str(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split(" - ", 1)[0].split()
                if len(fields) > 4 and fields[4] == target:
                    return True
    except OSError as exc:
        raise InfraManagerError("Невозможно проверить постоянное подключение 910") from exc
    return False


def _directory(path: Path) -> None:
    """Создать закрытый каталог или отклонить ссылку и посторонний тип."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise InfraManagerError(f"Нельзя создать каталог операций: {path}") from exc
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise InfraManagerError(f"Небезопасный каталог операций: {path}")
        if info.st_uid != os.geteuid():
            raise InfraManagerError(f"Неверный владелец каталога операций: {path}")
        path.chmod(0o700)
    except OSError as exc:
        raise InfraManagerError(f"Нет безопасного доступа к каталогу: {path}") from exc


def _read(path: Path) -> dict:
    """Открыть обычный файл без перехода по символьной ссылке."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
                raise InfraManagerError("Неверный тип или размер записи операции")
            chunks = bytearray()
            while len(chunks) <= MAX_RECORD_BYTES:
                block = os.read(fd, MAX_RECORD_BYTES + 1 - len(chunks))
                if not block:
                    break
                chunks.extend(block)
            if len(chunks) > MAX_RECORD_BYTES:
                raise InfraManagerError("Запись операции слишком велика")
        finally:
            os.close(fd)
        data = json.loads(chunks)
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data
    except (OSError, ValueError, UnicodeError) as exc:
        raise InfraManagerError(f"Запись операции недоступна или повреждена: {path.name}") from exc


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, data: dict) -> None:
    """Записать JSON атомарно, синхронизировать файл и каталог."""
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise InfraManagerError("Слишком большая запись операции")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_event(path: Path, operation_id: str, phase: str) -> None:
    """Сохранить только служебное событие, никогда не сырой вывод задания."""
    record = {"at": _now(), "id": operation_id, "phase": phase}
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    fd = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InfraManagerError("Неверный тип журнала операции")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _valid_id(value: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise InfraManagerError("Идентификатор задания должен содержать 32 знака UUID")
    return value


class OperationStore:
    """Единое закрытое хранилище запросов, результатов и служебных событий."""

    def __init__(
        self,
        root: Path = DEFAULT_ROOT,
        *,
        require_mount: bool = True,
    ) -> None:
        self.root = Path(root)
        if require_mount and (
            self.root != DEFAULT_ROOT or not _mounted(MOUNT_ROOT)
        ):
            raise InfraManagerError(
                "Нет подтверждённого подключения /mnt/persistent-state; "
                "запись в rootfs запрещена"
            )
        if not self.root.parent.is_dir() or self.root.parent.is_symlink():
            raise InfraManagerError("Отсутствует постоянный каталог состояния 910")
        for path in (self.root, self.active, self.history, self.logs, self.settings):
            _directory(path)

    @property
    def active(self) -> Path:
        return self.root / "active"

    @property
    def history(self) -> Path:
        return self.root / "history"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def settings(self) -> Path:
        return self.root / "settings"

    @contextmanager
    def _registry(self) -> Iterator[None]:
        """Одна межпроцессная блокировка для изменения каталога заданий."""
        descriptor = os.open(
            self.root / "registry.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise InfraManagerError("Некорректная блокировка заданий")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _history_path(self, operation_id: str) -> Path:
        return self.history / f"{_valid_id(operation_id)}.json"

    def _active_path(self, target: int) -> Path:
        if not isinstance(target, int) or isinstance(target, bool) or target < 1:
            raise InfraManagerError("Некорректный VMID операции")
        return self.active / f"{target}.json"

    def get(self, operation_id: str) -> dict:
        return _read(self._history_path(operation_id))

    def list_recent(self, limit: int = 20) -> list[dict]:
        """Вернуть последние задания без чтения произвольных файлов."""
        if not 1 <= limit <= 100:
            raise InfraManagerError("Неверный предел истории")
        items: list[dict] = []
        for path in self.history.iterdir():
            if not path.name.endswith(".json"):
                continue
            operation_id = path.name[:-5]
            if not ID_PATTERN.fullmatch(operation_id):
                continue
            items.append(_read(path))
        return sorted(items, key=lambda x: (x["created_at"], x["id"]), reverse=True)[:limit]

    def reserve(
        self,
        kind: str,
        target: int,
        revision: str,
        *,
        request_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Создать запрос или вернуть ровно тот же запрос без повторного пуска."""
        if kind not in KINDS:
            raise InfraManagerError(f"Недопустимое действие: {kind}")
        active_path = self._active_path(target)
        operation_id = _valid_id(request_id) if request_id is not None else uuid.uuid4().hex
        if not isinstance(revision, str) or not revision or len(revision) > 96:
            raise InfraManagerError("Не определена версия проекта для операции")
        with self._registry():
            history_path = self._history_path(operation_id)
            if history_path.exists() or history_path.is_symlink():
                previous = _read(history_path)
                if previous.get("kind") != kind or previous.get("target") != target:
                    raise InfraManagerError("Идентификатор уже принадлежит другой операции")
                return previous, False
            if active_path.exists() or active_path.is_symlink():
                active_record = _read(active_path)
                previous_id = _valid_id(active_record.get("id", ""))
                existing = self.get(previous_id)
                if existing.get("phase") in TERMINAL:
                    active_path.unlink()
                    _sync_dir(self.active)
                else:
                    raise InfraManagerError(
                        f"Для гостя {target} имеется незавершённое задание {previous_id}; "
                        "проверка необходима до повторного запуска"
                    )
            record = {
                "schema_version": 1,
                "id": operation_id,
                "kind": kind,
                "target": target,
                "revision": revision,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "phase": "queued",
                "result": None,
                "exit_code": None,
                "log": f"logs/{operation_id}.jsonl",
            }
            _atomic_json(history_path, record)
            _atomic_json(active_path, {"id": operation_id})
            _append_event(self.logs / f"{operation_id}.jsonl", operation_id, "queued")
            return record, True

    def start(self, operation_id: str) -> dict:
        """Зафиксировать исполнение только если запрос всё ещё активен."""
        with self._registry():
            record = self.get(operation_id)
            active_path = self._active_path(record["target"])
            if _read(active_path).get("id") != operation_id or record["phase"] != "queued":
                raise InfraManagerError("Нельзя начать уже выполненное или чужое задание")
            record.update(phase="running", started_at=_now())
            _atomic_json(self._history_path(operation_id), record)
            _append_event(self.logs / f"{operation_id}.jsonl", operation_id, "running")
            return record

    def finish(self, operation_id: str, *, exit_code: int) -> dict:
        """Записать конечный результат до снятия постоянной блокировки."""
        with self._registry():
            record = self.get(operation_id)
            active_path = self._active_path(record["target"])
            if _read(active_path).get("id") != operation_id or record["phase"] != "running":
                raise InfraManagerError("Итог не принадлежит действующей операции")
            successful = exit_code == 0
            phase = "completed" if successful else "failed"
            record.update(
                phase=phase,
                result="ok" if successful else "failed",
                exit_code=exit_code,
                finished_at=_now(),
            )
            _atomic_json(self._history_path(operation_id), record)
            _append_event(self.logs / f"{operation_id}.jsonl", operation_id, phase)
            active_path.unlink()
            _sync_dir(self.active)
            return record


def run_recorded(
    store: OperationStore,
    kind: str,
    target: int,
    revision: str,
    execute: Callable[[str, int], int],
    *,
    request_id: str | None = None,
) -> dict:
    """Выполнить разрешённое действие, не создавая повторного задания."""
    record, created = store.reserve(kind, target, revision, request_id=request_id)
    if not created:
        return record
    operation_id = record["id"]
    store.start(operation_id)
    code = 1
    try:
        code = execute(kind, target)
        if not isinstance(code, int):
            code = 1
    except (Exception, KeyboardInterrupt):
        code = 1
    return store.finish(operation_id, exit_code=code)
