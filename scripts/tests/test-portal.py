#!/usr/bin/env python3
"""Проверки локального портала гостей."""

from __future__ import annotations

import http.client
import json
import sys
import threading
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError
from infra_manager.portal import (
    build_portal_bookmarks,
    local_portal_definition,
    portal_services,
)
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


def check_service_discovery() -> None:
    infra = portal_services(910, "local")
    if {next(iter(item)) for item in infra} != {"Semaphore", "OpenBao"}:
        fail("Homepage 910 должен публиковать Semaphore и OpenBao")

    gateway = portal_services(109, "local")
    if {next(iter(item)) for item in gateway} != {"AdGuard Home"}:
        fail("Homepage 109 должен публиковать AdGuard Home")

    ai = portal_services(410, "local")
    if {next(iter(item)) for item in ai} != {"Open WebUI"}:
        fail("Homepage 410 должен публиковать Open WebUI")

    if {next(iter(item)) for item in portal_services(109, "home")} != {
        "AdGuard Home"
    }:
        fail("AdGuard Home должен быть доступен общему порталу квартиры")
    if {next(iter(item)) for item in portal_services(410, "home")} != {
        "Open WebUI"
    }:
        fail("Open WebUI должен быть доступен общему порталу квартиры")


def check_local_dashboard_address() -> None:
    expected = {
        109: "http://192.168.1.9:3001/",
        410: "http://192.168.4.10:3001/",
        910: "http://192.168.9.10:3001/",
    }
    for vmid, url in expected.items():
        identity = portal_gateway.guest_identity(ROOT, vmid)
        dashboard = local_portal_definition(ROOT, identity)
        if not isinstance(dashboard, dict) or dashboard.get("url") != url:
            fail(
                f"Гость {vmid} имеет неверный адрес локальной панели: "
                f"{dashboard!r}"
            )

    identity = portal_gateway.guest_identity(ROOT, 311)
    if local_portal_definition(ROOT, identity) is not None:
        fail("Гость 311 без portal.local не должен иметь локальную панель")


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


def _http_request(
    server: portal_gateway.PortalActionServer,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    body = response.read().decode("utf-8", errors="replace")
    response_headers = {key: value for key, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, response_headers, body


def check_gateway_http() -> None:
    server = portal_gateway.PortalActionServer(
        ("127.0.0.1", 0),
        "192.168.9.10",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        status, _, body = _http_request(server, "GET", "/health")
        if status != HTTPStatus.OK or body != "ok\n":
            fail("Health шлюза Homepage должен возвращать 200")

        with patch.object(
            portal_gateway,
            "_session_valid",
            return_value=False,
        ):
            status, _, body = _http_request(
                server,
                "GET",
                "/action/status?vmid=910",
            )
        if status != HTTPStatus.UNAUTHORIZED or "Требуется вход" not in body:
            fail("Без сессии Semaphore шлюз должен требовать вход")

        with patch.object(
            portal_gateway,
            "_session_valid",
            return_value=True,
        ):
            status, _, body = _http_request(
                server,
                "GET",
                "/action/status?vmid=910",
                headers={"Cookie": "semaphore=session-cookie"},
            )
        if status != HTTPStatus.OK or "910 — infra-manager" not in body:
            fail("GET действия должен показывать подтверждение и выбранный VMID")

        host, port = server.server_address
        same_origin = f"http://{host}:{port}"
        with (
            patch.object(
                portal_gateway,
                "_session_valid",
                return_value=True,
            ),
            patch.object(
                portal_gateway,
                "queue_action",
                return_value=(1, 64),
            ) as queue,
        ):
            status, headers, _ = _http_request(
                server,
                "POST",
                "/action/status?vmid=910",
                headers={
                    "Cookie": "semaphore=session-cookie",
                    "Origin": same_origin,
                },
            )
        if status != HTTPStatus.SEE_OTHER:
            fail("Подтверждённое действие должно создать задание Semaphore")
        if headers.get("Location") != (
            "http://192.168.9.10:3000/project/1/history?t=64"
        ):
            fail("После запуска шлюз должен открыть созданное задание Semaphore")
        queue.assert_called_once_with(
            "status",
            910,
            "semaphore=session-cookie",
        )

        with (
            patch.object(
                portal_gateway,
                "_session_valid",
                return_value=True,
            ),
            patch.object(portal_gateway, "queue_action") as queue,
        ):
            status, _, _ = _http_request(
                server,
                "POST",
                "/action/status?vmid=910",
                headers={
                    "Cookie": "semaphore=session-cookie",
                    "Origin": "http://example.invalid",
                },
            )
        if status != HTTPStatus.FORBIDDEN:
            fail("Шлюз должен отклонять POST с другого источника")
        queue.assert_not_called()

        status, _, _ = _http_request(
            server,
            "GET",
            "/action/delete?vmid=910",
        )
        if status != HTTPStatus.BAD_REQUEST:
            fail("Шлюз должен отклонять действие вне белого списка")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
    check_service_discovery()
    check_local_dashboard_address()
    check_action_validation()
    check_queue_payload()
    check_gateway_http()
    check_rejected_task()
    print("[ОК] Локальный портал гостей проверен")


if __name__ == "__main__":
    main()
