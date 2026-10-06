#!/usr/bin/env python3
"""Проверки локального портала гостей."""

from __future__ import annotations

import json
import sys
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError
from infra_manager.portal import build_portal_bookmarks
from infra_manager import portal_gateway


def fail(message: str) -> None:
    raise SystemExit(message)


def _labels(bookmarks: list[dict]) -> set[str]:
    if not bookmarks:
        return set()
    groups = bookmarks[0].get("Управление", [])
    return {next(iter(item)) for item in groups}


def check_bookmarks() -> None:
    infra = build_portal_bookmarks(910)
    if _labels(infra) != {"Проверить", "Синхронизировать", "Исправить"}:
        fail("Homepage 910 должен содержать три стандартных действия")

    gateway_port = portal_gateway.SETTINGS.portal_gateway_port
    serialized = json.dumps(infra, ensure_ascii=False)
    for path in ("status", "sync", "repair"):
        expected = f":{gateway_port}/action/{path}?vmid=910"
        if expected not in serialized:
            fail(f"В Homepage 910 отсутствует ссылка {expected}")

    gateway = build_portal_bookmarks(109)
    if _labels(gateway) != {"Проверить", "Синхронизировать", "Исправить"}:
        fail("Homepage 109 должен содержать три стандартных действия")


def check_action_validation() -> None:
    portal_gateway._validate_action("status", 910)

    portal_gateway._validate_action("sync", 109)

    try:
        portal_gateway._validate_action("deploy", 910)
    except InfraManagerError:
        pass
    else:
        fail("Посредник не должен принимать действие вне белого списка")


def check_queue_payload() -> None:
    response = (HTTPStatus.CREATED, b'{"id":64}')
    with (
        patch.object(
            portal_gateway,
            "_semaphore_objects",
            return_value=(1, 25),
        ),
        patch.object(
            portal_gateway,
            "_semaphore_request",
            return_value=response,
        ) as request,
    ):
        project_id, task_id = portal_gateway.queue_action(
            "status",
            910,
            "semaphore=session-cookie",
        )

    if (project_id, task_id) != (1, 64):
        fail("Посредник неверно обработал ответ Semaphore")

    args, kwargs = request.call_args
    if args != ("POST", "/project/1/tasks"):
        fail(f"Неверный адрес создания задания: {args!r}")
    if kwargs.get("cookie") != "semaphore=session-cookie":
        fail("Посредник должен передавать пользовательскую сессию Semaphore")

    payload = kwargs.get("payload")
    if not isinstance(payload, dict):
        fail("Посредник не передал JSON задания Semaphore")
    if payload.get("template_id") != 25:
        fail("Посредник использовал неверный шаблон Semaphore")
    environment = json.loads(payload.get("environment", "{}"))
    if environment != {"GUEST_VMID": "910"}:
        fail(f"Неверный GUEST_VMID: {environment!r}")


def check_rejected_task() -> None:
    with (
        patch.object(
            portal_gateway,
            "_semaphore_objects",
            return_value=(1, 25),
        ),
        patch.object(
            portal_gateway,
            "_semaphore_request",
            return_value=(HTTPStatus.FORBIDDEN, b"forbidden"),
        ),
    ):
        try:
            portal_gateway.queue_action(
                "status",
                910,
                "semaphore=session-cookie",
            )
        except InfraManagerError:
            pass
        else:
            fail("Отказ Semaphore должен оставаться ошибкой посредника")


def main() -> None:
    check_bookmarks()
    check_action_validation()
    check_queue_payload()
    check_rejected_task()
    print("[ОК] Локальный портал гостей проверен")


if __name__ == "__main__":
    main()
