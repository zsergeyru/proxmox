#!/usr/bin/env python3
"""Проверить безопасную первую настройку Homarr через локальный интерфейс.

Проверки не обращаются к настоящему Homarr: локальный сервер имитирует
его интерфейс версии 2.5.2 и возможные отказы. Постоянные данные живых
гостей при тестировании не затрагиваются.
"""

from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "automation/ansible/roles/web_gateway/files/initialize-homarr.py"


def require(value: bool, message: str) -> None:
    """Прервать проверку при нарушении поведения."""
    if not value:
        raise AssertionError(message)


def load_module():
    """Загрузить сценарий без вызова его командной строки."""
    spec = importlib.util.spec_from_file_location("homarr_init", SOURCE)
    require(spec is not None and spec.loader is not None, "Сценарий Homarr отсутствует")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HomarrState:
    """Хранить состояние и вызовы искусственного Homarr."""
    def __init__(self):
        self.step = "start"
        self.group = None
        self.claimed = False
        self.operations = []
        self.fail_setup = False


def start_server(state):
    """Запустить локальный интерфейс с проверкой одноразового cookie."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def answer(self, data, status=200, cookie=None):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlsplit(self.path).path == "/api/trpc/onboard.currentStep":
                self.answer({"result": {"data": {"json": {"current": state.step}}}})
            else:
                self.answer({"error": "not found"}, 404)

        def do_POST(self):
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length) or "{}")
            if path == "/api/onboarding/claim":
                state.claimed = True
                self.answer({"status": "issued"}, cookie="homarr-onboarding-claim=fake; Path=/")
                return
            if "homarr-onboarding-claim=fake" not in self.headers.get("Cookie", ""):
                self.answer({"error": "forbidden"}, 403)
                return
            if path == "/api/trpc/onboard.nextStep" and state.step == "start":
                state.operations.append(path)
                state.step = "group"
            elif path == "/api/trpc/group.createInitialExternalGroup" and state.step == "group":
                state.operations.append(path)
                state.group = value["json"]["name"]
                state.step = "setup"
            elif path == "/api/trpc/onboard.completeSetup" and state.step == "setup":
                if state.fail_setup:
                    self.answer({"error": "failure"}, 500)
                    return
                assert value["json"]["server"]["defaultLocale"] == "ru"
                assert value["json"]["board"]["name"] == "dashboard"
                assert value["json"]["apps"] == []
                state.operations.append(path)
                state.step = "finish"
            else:
                self.answer({"error": "invalid step"}, 400)
                return
            self.answer({"result": {"data": {"json": None}}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test() -> None:
    """Проверить создание группы, повторный запуск и отказ без отметки."""
    module = load_module()
    with TemporaryDirectory() as tempdir:
        marker = Path(tempdir) / "homarr-ready"
        state = HomarrState()
        server, thread = start_server(state)
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            require(not marker.exists(), "Изначально готовность Homarr не подтверждена")
            result = module.initialize(url, marker, "admins")
            require("завершена" in result, "Не показан результат настройки")
            require(state.group == "admins" and state.step == "finish",
                    "Должна быть создана группа OIDC и закрытая доска")
            require(marker.read_text().strip() == "ready", "Нет отметки готовности")
            require(marker.stat().st_mode & 0o777 == 0o644,
                    "Отметка должна читаться в Caddy")
            first_operations = list(state.operations)
            module.initialize(url, marker, "admins")
            require(state.operations == first_operations,
                    "Повторный запуск не должен повторять операции создания")
            require(marker.exists(), "Готовое состояние после проверки должно сохраниться")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    with TemporaryDirectory() as tempdir:
        marker = Path(tempdir) / "homarr-ready"
        marker.write_text("stale")
        state = HomarrState()
        state.fail_setup = True
        server, thread = start_server(state)
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                module.initialize(url, marker, "admins")
            except module.OnboardingError:
                pass
            else:
                raise AssertionError("Неудачная настройка не должна объявляться успешной")
            require(not marker.exists(), "После ошибки публичный доступ должен остаться закрытым")
            require(state.step == "setup", "Первая группа уже создана и должна сохраниться")
            state.fail_setup = False
            module.initialize(url, marker, "admins")
            require(state.step == "finish" and marker.exists(),
                    "Повторный запуск должен продолжить настройку без потери группы")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    # Если создание закрытой доски не получилось, нельзя помечать Homarr готовым.
    with TemporaryDirectory() as tempdir:
        marker = Path(tempdir) / "homarr-ready"
        state = HomarrState()
        state.step = "finish"
        server, thread = start_server(state)
        try:
            from unittest.mock import patch
            from types import SimpleNamespace

            with patch.object(
                module.subprocess, "run",
                return_value=SimpleNamespace(returncode=1),
            ):
                try:
                    module.initialize(
                        f"http://127.0.0.1:{server.server_port}",
                        marker, "admins", ["python3", "invalid-bootstrap"],
                    )
                except module.OnboardingError:
                    pass
                else:
                    raise AssertionError("При ошибке создания виджетов нужен отказ")
            require(not marker.exists(),
                    "Нельзя показывать публичный Homarr без административной доски")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    try:
        module.HomarrClient("http://192.168.1.40:7575")
    except module.OnboardingError:
        pass
    else:
        raise AssertionError("Нельзя вызывать первичную настройку через внешнюю сеть")
    print("140: Homarr создаётся один раз; ошибки не открывают публичный доступ")


if __name__ == "__main__":
    test()
