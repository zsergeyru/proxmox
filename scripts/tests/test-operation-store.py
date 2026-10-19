#!/usr/bin/env python3
"""Проверки постоянных операций 910 без PVE, Docker и Semaphore."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.operation_store import OperationStore, run_recorded
from infra_manager import operation_runner


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def fails(fn, label: str) -> None:
    try:
        fn()
    except InfraManagerError:
        return
    raise AssertionError(label)


def _child_reserve(root: str, reply: multiprocessing.Queue) -> None:
    try:
        store = OperationStore(Path(root), require_mount=False)
        store.reserve("sync", 140, "abc1234")
        reply.put("created")
    except InfraManagerError:
        reply.put("blocked")


def check_persistence_and_idempotency(root: Path) -> None:
    store = OperationStore(root, require_mount=False)
    request_id = uuid.uuid4().hex
    record, created = store.reserve(
        "status", 109, "a1b2c3d", request_id=request_id
    )
    check(created and record["phase"] == "queued", "Запрос должен быть сохранён")
    check(record["id"] == request_id, "Нельзя менять идентификатор запроса")
    same, created = store.reserve("status", 109, "a1b2c3d", request_id=request_id)
    check(not created and same == record, "Повторный запрос создал новое задание")
    fails(
        lambda: store.reserve("deploy", 109, "a1b2c3d", request_id=request_id),
        "Идентификатор должен быть привязан к исходному действию",
    )
    fails(
        lambda: store.reserve("status", 109, "changed", request_id=request_id),
        "Идентификатор должен быть привязан к версии проекта",
    )
    fails(
        lambda: store.reserve("status", 109, "a1b2c3d"),
        "Нельзя запускать второе задание с незавершённым первым",
    )
    started = store.start(request_id)
    check(started["phase"] == "running", "Не записано начало операции")
    again = OperationStore(root, require_mount=False)
    check(again.get(request_id)["phase"] == "running", "Потеряна история при перезапуске")
    fails(
        lambda: again.start(request_id),
        "Не разрешено дважды запускать одну операцию",
    )
    final = store.finish(request_id, exit_code=0)
    check(final["result"] == "ok" and final["phase"] == "completed", "Нет подтверждённого итога")
    check(not (store.active / "109.json").exists(), "Активная запись не освобождена")
    check(again.get(request_id)["phase"] == "completed", "История исчезла после завершения")
    check(len(store.list_recent()) == 1, "Не сохранилась история")
    check(
        (store.logs / f"{request_id}.jsonl").is_file(),
        "Нет закрытого журнала этапов",
    )
    for line in (store.logs / f"{request_id}.jsonl").read_text().splitlines():
        check(set(json.loads(line)) == {"at", "id", "phase"}, "Журнал содержит чужие поля")
    for directory in (store.root, store.active, store.history, store.logs, store.settings):
        check(stat.S_IMODE(directory.stat().st_mode) == 0o700, "Небезопасные права каталога")
    check(
        stat.S_IMODE((store.history / f"{request_id}.json").stat().st_mode) == 0o600,
        "Небезопасные права записи",
    )


def check_activation_phases(root: Path) -> None:
    store = OperationStore(root, require_mount=False)
    record, _ = store.reserve("deploy", 109, "abc")
    operation_id = record["id"]
    store.start(operation_id)
    check(store.get(operation_id)["result"] is None, "Установка преждевременно успешна")
    store.transition(operation_id, "activating")
    store.transition(operation_id, "verifying")
    check(store.get(operation_id)["phase"] == "verifying", "Нет этапа проверки")
    check(store.get(operation_id)["result"] is None, "Проверка ещё не подтверждена")
    fails(
        lambda: store.transition(operation_id, "activating"),
        "Нельзя вернуться от проверки к предыдущему этапу",
    )
    final = store.finish(operation_id, exit_code=7)
    check(final["phase"] == "failed", "Ошибка проверки должна сохраняться")
    check(final["exit_code"] == 7, "Потерян код ошибки активации")
    fails(
        lambda: store.finish(operation_id, exit_code=0),
        "Нельзя заменить подтверждённую ошибку последующим успехом",
    )


def check_failure_and_duplicates(root: Path) -> None:
    store = OperationStore(root, require_mount=False)
    calls = []
    request_id = uuid.uuid4().hex

    def fail_action(kind: str, target: int) -> int:
        calls.append((kind, target))
        return 3

    result = run_recorded(
        store, "sync", 340, "abc", fail_action, request_id=request_id
    )
    check(result["phase"] == "failed" and result["exit_code"] == 3, "Ошибка не сохранена")
    result = run_recorded(
        store, "sync", 340, "abc", fail_action, request_id=request_id
    )
    check(len(calls) == 1, "Повторный запрос заново изменил гостя")
    check(result["phase"] == "failed", "Повторный запрос скрыл старый отказ")
    record, fresh = store.reserve("sync", 340, "abc")
    check(fresh and record["phase"] == "queued", "Ошибка блокирует все следующие операции")
    store.start(record["id"])
    # Имитируем аварийное завершение после старта: никто не подтверждал результат.
    resumed = OperationStore(root, require_mount=False)
    fails(
        lambda: resumed.reserve("sync", 340, "abc"),
        "После падения нельзя молча возобновлять или дублировать задание",
    )
    check(resumed.get(record["id"])["result"] is None, "Появился ложный успех")
    resumed.finish(record["id"], exit_code=1)


def check_process_lock(root: Path) -> None:
    store = OperationStore(root, require_mount=False)
    record, _ = store.reserve("deploy", 140, "abc")
    queue: multiprocessing.Queue = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=_child_reserve, args=(str(root), queue)
    )
    process.start()
    process.join(timeout=5)
    check(process.exitcode == 0, "Дочерний процесс не завершился")
    check(queue.get(timeout=5) == "blocked", "Межпроцессная защита от дубликатов не работает")
    store.start(record["id"])
    store.finish(record["id"], exit_code=0)


def check_safety(root: Path) -> None:
    fails(lambda: OperationStore(), "Без подтверждённого mount нельзя создать состояние")
    store = OperationStore(root, require_mount=False)
    fails(lambda: store.reserve("shell", 109, "abc"), "Нельзя выполнить произвольную команду")
    fails(lambda: store.reserve("deploy", 0, "abc"), "VMID 0 запрещён")
    fails(lambda: store.get("../secrets"), "Недопустимый идентификатор позволил обход пути")
    fails(lambda: store.list_recent(limit=1000), "Нет ограничения списка")
    symlink = store.history / f"{uuid.uuid4().hex}.json"
    symlink.symlink_to("/etc/passwd")
    fails(lambda: store.get(symlink.stem), "Символьная ссылка прочитана как запись")
    symlink.unlink()
    badroot = root.parent / "link-operations"
    badroot.symlink_to(root, target_is_directory=True)
    fails(
        lambda: OperationStore(badroot, require_mount=False),
        "Символьная ссылка на корень должна быть запрещена",
    )
    with patch("infra_manager.operation_store._mounted", return_value=False):
        fails(
            lambda: OperationStore(),
            "Отсутствие bind mount должно останавливать запись",
        )


def check_runner() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        store = OperationStore(Path(temporary) / "operations", require_mount=False)
        with (
            patch.object(operation_runner, "OperationStore", return_value=store),
            patch.object(operation_runner, "_allowed") as validate,
            patch.object(operation_runner, "_execute", return_value=0) as execute,
            patch.object(operation_runner, "project_revision", return_value="12345abc"),
            patch.object(operation_runner.os, "geteuid", return_value=0),
        ):
            request_id = uuid.uuid4().hex
            command = ["run", "sync", "109", "--request-id", request_id]
            check(operation_runner.main(command) == 0, "CLI не завершил разрешённое задание")
            check(operation_runner.main(command) == 0, "Повтор CLI изменил результат")
            check(execute.call_count == 1, "CLI выполнил дублирующую команду")
            check(validate.call_count == 2, "Проверка разрешений должна работать на каждом запросе")
            check(operation_runner.main(["show", request_id]) == 0, "Не работает просмотр")
            check(operation_runner.main(["list", "--limit", "2"]) == 0, "Не работает история")


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        check_persistence_and_idempotency(base / "a")
        check_activation_phases(base / "activation")
        check_failure_and_duplicates(base / "b")
        check_process_lock(base / "c")
        check_safety(base / "d")
    check_runner()
    print("[ОК] Защищённое постоянное состояние операций и единый вход проверены")


if __name__ == "__main__":
    main()
