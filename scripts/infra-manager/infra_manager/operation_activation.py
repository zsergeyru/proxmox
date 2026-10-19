"""Внутреннее подтверждение systemd-активации управляющего гостя.

Не является пользовательской командой. Активная запись проверяется
хранилищем по идентификатору и цели, без доверия к временному маркеру.
"""

from __future__ import annotations

import argparse
import os
import sys

from .common import InfraManagerError
from .operation_store import OperationStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infra-manager activation-internal")
    parser.add_argument("action", choices=("verifying", "finish", "reconcile"))
    parser.add_argument("operation_id", nargs="?")
    parser.add_argument("exit_code", type=int, nargs="?")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InfraManagerError("Подтверждение активации требует полномочий системной службы")
        store = OperationStore()
        if args.action == "reconcile":
            if args.operation_id is not None or args.exit_code is not None:
                raise InfraManagerError("Проверка прерванных заданий не принимает параметры")
            for operation_id in store.reconcile_interrupted():
                print(f"[ПРОВЕРИТЬ] Прерванное задание: {operation_id}", flush=True)
            return 0
        if args.operation_id is None:
            raise InfraManagerError("Не указан идентификатор операции")
        record = store.get(args.operation_id)
        if record.get("kind") != "deploy":
            raise InfraManagerError("Идентификатор не относится к обновлению гостя")
        if args.action == "verifying":
            if args.exit_code is not None:
                raise InfraManagerError("Проверка не принимает код завершения")
            if record["phase"] == "running":
                store.transition(args.operation_id, "activating")
            if store.get(args.operation_id)["phase"] == "activating":
                store.transition(args.operation_id, "verifying")
        else:
            if args.exit_code is None or not 0 <= args.exit_code <= 255:
                raise InfraManagerError("Некорректный код завершения активации")
            store.finish(args.operation_id, exit_code=args.exit_code)
        return 0
    except (InfraManagerError, OSError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
