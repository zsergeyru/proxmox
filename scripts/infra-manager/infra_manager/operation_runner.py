"""Операторский вход в единое постоянное хранилище операций управляющего гостя.

Пока не завершён переход с Semaphore, новый путь запускается явно через
'operations run'. Обычные команды 'deploy/sync/repair/status' не меняются.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid

from .common import InfraManagerError
from .guest_catalog import guest_identity
from .guest_deploy import project_revision
from .guest_operations import operation_guests
from .operation_store import OperationStore, run_recorded
from .settings import PATHS, SETTINGS


def _allowed(kind: str, vmid: int) -> None:
    """Повторно проверить команду и VMID по единому машинному каталогу."""
    supported = {item.vmid for item in operation_guests(PATHS.repo_root, kind)}
    if vmid not in supported:
        raise InfraManagerError(f"Операция {kind} не разрешена для гостя {vmid}")
    identity = guest_identity(PATHS.repo_root, vmid)
    if kind == "deploy" and identity.role == SETTINGS.infra_manager_role:
        raise InfraManagerError(
            "Самообновление управляющего гостя пока доступно только штатной командой: "
            "подтверждение независимой активации относится к следующему этапу"
        )


def _execute(kind: str, vmid: int) -> int:
    """Использовать действующий общий оператор, а не второй развёртыватель."""
    from .operator import operator_guest_status, operator_guest_task

    if kind == "status":
        return operator_guest_status(vmid, show_secrets=False)
    return operator_guest_task(kind, vmid, show_secrets=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra-manager operations",
        description="Постоянные задания infra-manager вне базы Semaphore",
    )
    options = parser.add_subparsers(dest="action", required=True)
    runner = options.add_parser("run", help="выполнить разрешённое действие над гостем")
    runner.add_argument("kind", choices=("status", "sync", "repair", "deploy"))
    runner.add_argument("vmid", type=int)
    runner.add_argument("--request-id", help="UUID одного запроса без дефисов")
    viewer = options.add_parser("show", help="показать результат по идентификатору")
    viewer.add_argument("operation_id")
    recent = options.add_parser("list", help="показать историю заданий")
    recent.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполнить только разрешённое действие с постоянным учётом результата."""
    args = build_parser().parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InfraManagerError("Управление постоянными заданиями разрешено только root управляющего гостя")
        if args.action == "run":
            if args.vmid <= 0:
                raise InfraManagerError("VMID должен быть положительным числом")
            _allowed(args.kind, args.vmid)
            store = OperationStore()
            operation_id = args.request_id or uuid.uuid4().hex
            print(f"Идентификатор операции: {operation_id}", flush=True)
            result = run_recorded(
                store,
                args.kind,
                args.vmid,
                project_revision(PATHS.repo_root),
                _execute,
                request_id=operation_id,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            # В том числе повторный запрос к незавершённой операции
            # никогда не сообщает о подтверждённом успехе.
            if result["phase"] == "completed" and result["result"] == "ok":
                return 0
            return 2 if result["phase"] in {"queued", "running", "activating", "verifying"} else 1
        store = OperationStore()
        if args.action == "show":
            print(json.dumps(store.get(args.operation_id), ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(store.list_recent(limit=args.limit), ensure_ascii=False, sort_keys=True))
        return 0
    except (InfraManagerError, OSError) as exc:
        print(f"ОШИБКА: {exc}", file=__import__("sys").stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
