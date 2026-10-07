#!/usr/bin/env python3
"""Проверки локального портала гостей."""

from __future__ import annotations

import http.client
import io
import json
import sys
import tempfile
import threading
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError
from infra_manager.guest_catalog import guest_identity
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


def _service(items: list[dict], name: str) -> dict:
    for item in items:
        if name in item:
            value = item[name]
            if isinstance(value, dict):
                return value
    fail(f"Не найдена служба Homepage {name}")
    return {}


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

    for icon in (
        "mdi-check-circle-outline-#34d399",
        "mdi-sync-#38bdf8",
        "mdi-wrench-#fb923c",
    ):
        if icon not in serialized:
            fail(f"В Homepage 910 отсутствует иконка действия {icon}")

    for description in (
        "Проверка состояния",
        "Обновление конфигурации",
        "Устранение проблем",
    ):
        if description not in serialized:
            fail(f"В Homepage 910 отсутствует описание действия {description}")

    gateway = build_portal_bookmarks(109)
    if _labels(gateway) != {"Проверить", "Синхронизировать", "Исправить"}:
        fail("Homepage 109 должен содержать три стандартных действия")


def check_service_discovery() -> None:
    infra = portal_services(910, "local")
    if {next(iter(item)) for item in infra} != {"Semaphore", "OpenBao"}:
        fail("Homepage 910 должен публиковать Semaphore и OpenBao")
    if _service(infra, "Semaphore").get("icon") != "mdi-server-#38bdf8":
        fail("Semaphore должен иметь цветную иконку")
    if _service(infra, "Semaphore").get("description") != "Задачи и автоматизация":
        fail("Semaphore должен иметь описание")
    if _service(infra, "OpenBao").get("icon") != "mdi-lock-#e0f2fe":
        fail("OpenBao должен иметь цветную иконку")
    if _service(infra, "OpenBao").get("description") != "Хранилище секретов":
        fail("OpenBao должен иметь описание")
    if _service(infra, "OpenBao").get("siteMonitor") != (
        "https://192.168.9.10:8202/ui/"
    ):
        fail("OpenBao должен проверяться по HTTPS через siteMonitor")

    gateway = portal_services(109, "local")
    if {next(iter(item)) for item in gateway} != {"AdGuard Home"}:
        fail("Homepage 109 должен публиковать AdGuard Home")
    if _service(gateway, "AdGuard Home").get("icon") != "adguard-home.png":
        fail("AdGuard Home должен иметь иконку")
    if _service(gateway, "AdGuard Home").get("description") != (
        "DNS, DHCP и фильтрация"
    ):
        fail("AdGuard Home должен иметь описание")

    ai = portal_services(410, "local")
    if {next(iter(item)) for item in ai} != {"Open WebUI"}:
        fail("Homepage 410 должен публиковать Open WebUI")
    if _service(ai, "Open WebUI").get("icon") != "mdi-robot-#38bdf8":
        fail("Open WebUI должен иметь цветную иконку")
    if _service(ai, "Open WebUI").get("description") != "Веб-интерфейс ИИ":
        fail("Open WebUI должен иметь описание")

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
        identity = guest_identity(ROOT, vmid)
        dashboard = local_portal_definition(ROOT, identity)
        if not isinstance(dashboard, dict) or dashboard.get("url") != url:
            fail(
                f"Гость {vmid} имеет неверный адрес локальной панели: "
                f"{dashboard!r}"
            )
        if vmid == 910 and dashboard.get("trusted_ca") != (
            "/etc/infra-manager/openbao/tls/ca.crt"
        ):
            fail("Homepage 910 должен доверять TLS CA OpenBao")

    identity = guest_identity(ROOT, 311)
    if local_portal_definition(ROOT, identity) is not None:
        fail("Гость 311 без portal.local не должен иметь локальную панель")


def check_style_contract() -> None:
    role = ROOT / "automation" / "ansible" / "roles" / "portal"
    settings = (
        role / "templates" / "settings.yaml.j2"
    ).read_text(encoding="utf-8")
    expected = (
        "theme: dark",
        "color: slate",
        "headerStyle: clean",
        "iconStyle: theme",
        "cardBlur: md",
        "useEqualHeights: true",
        "disableCollapse: true",
        "image: /images/background.svg",
        "opacity: 65",
        "hideVersion: true",
        "columns: 2",
        "columns: 3",
        "icon: mdi-apps-#38bdf8",
        "icon: mdi-tools-#e2e8f0",
    )
    for value in expected:
        if value not in settings:
            fail(f"Единый стиль Homepage не содержит {value!r}")

    background = role / "files" / "background.svg"
    if not background.is_file() or background.stat().st_size < 200:
        fail("Не найден локальный фон Homepage")
    background_text = background.read_text(encoding="utf-8")
    if 'id="hexGrid"' not in background_text:
        fail("Фон Homepage должен содержать технологичную шестигранную сетку")

    custom_css = role / "templates" / "custom.css.j2"
    if not custom_css.is_file():
        fail("Не найден шаблон custom.css Homepage")
    css = custom_css.read_text(encoding="utf-8")
    if "title | to_json }}" in css:
        fail("Заголовок Homepage не должен кодировать кириллицу как \\uXXXX")

    for value in (
        "#layout-groups::before",
        "max-width: 1360px",
        "content: {{ provision.portal.local.title | to_json(ensure_ascii=False) }}",
        "min-height: 92px",
        "min-height: 88px",
        "rgba(52, 211, 153, 0.9)",
        "rgba(56, 189, 248, 0.9)",
        "rgba(251, 146, 60, 0.92)",
        'content: "Гость {{ provision.guest_vmid }}"',
    ):
        if value not in css:
            fail(f"Неоновая тема Homepage не содержит {value!r}")

    tasks = (role / "tasks" / "main.yml").read_text(encoding="utf-8")
    if "src: custom.css.j2" not in tasks or "ansible.builtin.template" not in tasks:
        fail("Оформление Homepage должно формироваться из шаблона custom.css.j2")

    compose = (
        role / "templates" / "docker-compose.yml.j2"
    ).read_text(encoding="utf-8")
    if "/app/public/images:ro" not in compose:
        fail("Локальные изображения Homepage не подключены в контейнер")
    if "NODE_EXTRA_CA_CERTS" not in compose:
        fail("Homepage не умеет подключать дополнительный TLS CA")
    if "/etc/ssl/homepage/trusted-ca.crt:ro" not in compose:
        fail("Доверенный TLS CA Homepage не подключён в контейнер")


def check_action_validation() -> None:
    portal_gateway._validate_action("status", 910)

    portal_gateway._validate_action("sync", 109)

    try:
        portal_gateway._validate_action("deploy", 910)
    except InfraManagerError:
        pass
    else:
        fail("Посредник не должен принимать действие вне белого списка")


def check_direct_action_start() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        command = Path(tmp) / "infra-manager"
        command.write_text("#!/bin/sh\n", encoding="utf-8")
        process = SimpleNamespace(stdout=io.StringIO(""), wait=lambda: 0)
        with (
            patch.object(portal_gateway, "OPERATOR_COMMAND", command),
            patch.object(
                portal_gateway.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
        ):
            if portal_gateway.start_action("sync", 109) is not process:
                fail("Посредник должен вернуть прямой операторский процесс")

    argv = popen.call_args.args[0]
    if argv != [str(command), "sync", "109"]:
        fail(f"Посредник запускает неверную команду: {argv!r}")
    if "--trusted-pve" in argv:
        fail("Homepage не должен получать доверенный режим вывода секретов")
    kwargs = popen.call_args.kwargs
    if kwargs.get("stdout") is not portal_gateway.subprocess.PIPE:
        fail("Прямой запуск должен передавать stdout в страницу результата")
    if kwargs.get("stderr") is not portal_gateway.subprocess.STDOUT:
        fail("stderr прямого запуска должен попадать в тот же журнал")
    if kwargs.get("env", {}).get("PYTHONUNBUFFERED") != "1":
        fail("Вывод прямого запуска должен быть построчным без буферизации")


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


def _fake_process(output: str, returncode: int = 0):
    return SimpleNamespace(
        stdout=io.StringIO(output),
        wait=lambda: returncode,
    )


def check_gateway_http() -> None:
    server = portal_gateway.PortalActionServer(
        ("127.0.0.1", 0),
        portal_gateway.PortalActionHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        status, _, body = _http_request(server, "GET", "/health")
        if status != HTTPStatus.OK or body != "ok\n":
            fail("Health шлюза Homepage должен возвращать 200")

        status, _, body = _http_request(
            server,
            "GET",
            "/action/status?vmid=910",
        )
        if status != HTTPStatus.OK:
            fail("GET действия должен быть доступен без Semaphore")
        if "910 — infra-manager" not in body or "Журнал появится" not in body:
            fail("GET действия должен показывать подтверждение прямого запуска")
        if "Semaphore" in body:
            fail("Страница подтверждения не должна зависеть от Semaphore")

        host, port = server.server_address
        same_origin = f"http://{host}:{port}"
        with patch.object(
            portal_gateway,
            "start_action",
            return_value=_fake_process(
                "[ИНФО] Проверка\n<опасный вывод>\n[ОК] Готово\n"
            ),
        ) as start:
            status, headers, body = _http_request(
                server,
                "POST",
                "/action/status?vmid=910",
                headers={"Origin": same_origin},
            )

        if status != HTTPStatus.OK:
            fail("Подтверждённое действие должно выполняться напрямую")
        if "Location" in headers:
            fail("Прямое действие Homepage не должно перенаправлять в Semaphore")
        if "&lt;опасный вывод&gt;" not in body or "<опасный вывод>" in body:
            fail("Журнал прямого запуска должен экранировать HTML")
        if "Операция завершена успешно" not in body:
            fail("Страница должна показывать успешный итог операции")
        if "общий исполнитель infra-manager" not in body:
            fail("Страница результата должна обозначать прямой запуск")
        start.assert_called_once_with("status", 910)

        with patch.object(portal_gateway, "start_action") as start:
            status, _, _ = _http_request(
                server,
                "POST",
                "/action/status?vmid=910",
                headers={"Origin": "http://example.invalid"},
            )
        if status != HTTPStatus.FORBIDDEN:
            fail("Шлюз должен отклонять POST с другого источника")
        start.assert_not_called()

        with patch.object(portal_gateway, "start_action") as start:
            status, _, _ = _http_request(
                server,
                "POST",
                "/action/status?vmid=910",
            )
        if status != HTTPStatus.FORBIDDEN:
            fail("Шлюз должен требовать Origin для изменяющего запроса")
        start.assert_not_called()

        with patch.object(
            portal_gateway,
            "start_action",
            return_value=_fake_process("[ОШИБКА] Проверка\n", returncode=7),
        ):
            status, _, body = _http_request(
                server,
                "POST",
                "/action/repair?vmid=910",
                headers={"Origin": same_origin},
            )
        if status != HTTPStatus.OK or "кодом 7" not in body:
            fail("Страница должна показывать код ошибки прямой операции")

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


def main() -> None:
    check_bookmarks()
    check_service_discovery()
    check_local_dashboard_address()
    check_style_contract()
    check_action_validation()
    check_direct_action_start()
    check_gateway_http()
    print("[ОК] Локальный портал гостей проверен")


if __name__ == "__main__":
    main()
