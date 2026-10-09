#!/usr/bin/env python3
"""PVE: ограниченный исполнитель команд infra-manager для гостя 410.

Запускается только через узкое sudo-правило, без доступа к shell PVE.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

POLICY = Path("/etc/infra-manager/agent-policy.json")
APPROVALS = Path("/run/infra-manager/agent-approvals")
INFRA_MANAGER = "/usr/local/sbin/infra-manager"
OPERATIONS = frozenset({"guests", "status", "test", "deploy", "sync", "repair"})
MUTATING = frozenset({"deploy", "sync", "repair"})
APPROVAL_TTL = 300


class AgentAccessError(RuntimeError):
    pass


def audit(message: str) -> None:
    """Записать факт вызова без аргументов, способных содержать секреты."""
    logger = logging.getLogger("infra-manager-agent")
    logger.setLevel(logging.INFO)
    if not logger.handlers and Path("/dev/log").exists():
        logger.addHandler(logging.handlers.SysLogHandler(address="/dev/log"))
    logger.info("infra-manager-agent: %s", message)


def _safe_root_file(path: Path) -> None:
    if path.is_symlink():
        raise AgentAccessError("Символьная ссылка вместо конфигурации запрещена")
    try:
        info = path.stat()
    except OSError as exc:
        raise AgentAccessError("Конфигурация агентского доступа отсутствует") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022):
        raise AgentAccessError("Небезопасные права файла агентского доступа")


def load_policy() -> dict[str, object]:
    _safe_root_file(POLICY)
    try:
        payload = json.loads(POLICY.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentAccessError("Некорректная политика агентского доступа") from exc
    if (not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("subject") != "guest:410"
            or not isinstance(payload.get("allowed"), dict)):
        raise AgentAccessError("Политика агентского доступа не соответствует контракту")
    return payload["allowed"]


def validate_command(arguments: list[str], allowed: dict[str, object]) -> tuple[str, int | None]:
    if not arguments or arguments[0] not in OPERATIONS:
        raise AgentAccessError("Операция запрещена")
    operation = arguments[0]
    if operation == "guests":
        if arguments != ["guests"] or allowed.get("guests") is not True:
            raise AgentAccessError("Операция guests запрещена")
        return operation, None
    if operation == "status" and arguments == ["status"]:
        if allowed.get("status") != "all":
            raise AgentAccessError("Общий статус запрещён")
        return operation, None
    if len(arguments) != 2 or not arguments[1].isascii() or not arguments[1].isdecimal():
        raise AgentAccessError("Ожидается точный числовой VMID")
    vmid = int(arguments[1])
    if vmid < 100 or vmid > 999999:
        raise AgentAccessError("Некорректный VMID")
    if operation == "status" and allowed.get("status") == "all":
        return operation, vmid
    targets = allowed.get(operation)
    if (not isinstance(targets, list) or any(type(item) is not int for item in targets)
            or vmid not in targets):
        raise AgentAccessError("Команда или VMID не разрешены")
    if operation in MUTATING and vmid in {100, 410, 910}:
        raise AgentAccessError("Запрещено изменять защищённого гостя")
    return operation, vmid


def _secure_directory() -> None:
    if APPROVALS.is_symlink():
        raise AgentAccessError("Недопустимое хранилище подтверждений")
    APPROVALS.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = APPROVALS.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise AgentAccessError("Небезопасные права на хранилище подтверждений")


def _approval_path(operation: str, vmid: int) -> Path:
    return APPROVALS / f"{operation}-{vmid}.json"


def _approval_lock():
    fd = os.open(APPROVALS / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def issue_approval(operation: str, vmid: int, policy: dict[str, object]) -> None:
    validate_command([operation, str(vmid)], policy)
    if operation not in MUTATING:
        raise AgentAccessError("Подтверждения требуются только для изменений")
    _secure_directory()
    fd = _approval_lock()
    try:
        path = _approval_path(operation, vmid)
        temp = APPROVALS / f".approval-{os.getpid()}"
        file_fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
                json.dump({"expires_at": time.time() + APPROVAL_TTL}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    finally:
        os.close(fd)
    audit(f"approved {operation} {vmid} by PVE root")


def consume_approval(operation: str, vmid: int) -> None:
    _secure_directory()
    fd = _approval_lock()
    try:
        path = _approval_path(operation, vmid)
        _safe_root_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        finally:
            path.unlink(missing_ok=True)
        expires = payload.get("expires_at") if isinstance(payload, dict) else None
        if type(expires) not in (int, float) or expires <= time.time():
            raise AgentAccessError("Подтверждение отсутствует или истекло")
    finally:
        os.close(fd)


def execute(arguments: list[str]) -> int:
    allowed = load_policy()
    operation, vmid = validate_command(arguments, allowed)
    if operation in MUTATING:
        assert vmid is not None
        consume_approval(operation, vmid)
    audit(f"start guest:410 {operation} {vmid if vmid is not None else '-'}")
    try:
        # --agent отключает доверенный вывод паролей PVE, обычные права root не передаются.
        result = subprocess.run([INFRA_MANAGER, "--agent", *arguments], check=False)
    except OSError as exc:
        raise AgentAccessError("Не удалось запустить infra-manager") from exc
    audit(f"finish guest:410 {operation} {vmid if vmid is not None else '-'} exit={result.returncode}")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ограниченный вызов infra-manager на PVE")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("--execute").add_argument("args", nargs="*")
    approval = sub.add_parser("--approve")
    approval.add_argument("operation", choices=sorted(MUTATING))
    approval.add_argument("vmid", type=int)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise AgentAccessError("Ограниченный исполнитель требует root через sudo")
    policy = load_policy()
    if args.mode == "--approve":
        # sudoers разрешает только --execute; утверждение возможно лишь из root PVE.
        if os.getuid() != 0:
            raise AgentAccessError("Подтверждать изменения может только root PVE")
        issue_approval(args.operation, args.vmid, policy)
        print(f"Подтверждено: {args.operation} {args.vmid} (один запуск, {APPROVAL_TTL // 60} мин)")
        return 0
    return execute(args.args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AgentAccessError as exc:
        audit("denied operation")
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        raise SystemExit(1)
