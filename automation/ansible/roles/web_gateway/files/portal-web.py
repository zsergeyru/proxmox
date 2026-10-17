#!/usr/bin/env python3
"""Закрытый веб-интерфейс 140; доступен только Caddy по Unix-сокету.

Caddy выполняет проверку Authelia, второго фактора, локальной сети и группы.
Здесь проверяются независимые признаки пропуска, происхождение изменяющего
запроса и его формат; служебный ключ никогда не выдаётся браузеру.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
import socketserver

ROOT = Path("/var/lib/web-gateway")
SOCKET = ROOT / "management/portal-socket/portal.sock"
ADMIN_SOCKET = ROOT / "management/admin-socket/control.sock"
ADMIN_TOKEN = ROOT / "secrets/administration-token"
CATALOG = ROOT / "management/catalog"
ORIGIN = "https://home.zsergey.ru"
MAX_BODY = 4096


class UnixConnection(HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=8)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def upstream(path: str, method: str, body: bytes | None = None) -> tuple[int, bytes]:
    if ADMIN_TOKEN.is_symlink() or not ADMIN_TOKEN.is_file() or ADMIN_TOKEN.stat().st_mode & 0o077:
        raise RuntimeError("Нет защищённого ключа управления")
    token = ADMIN_TOKEN.read_text(encoding="utf-8").strip()
    if len(token) < 40:
        raise RuntimeError("Повреждён ключ управления")
    connection = UnixConnection(ADMIN_SOCKET)
    try:
        connection.request(
            method, path, body=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        result = connection.getresponse()
        if result.status not in {200, 400, 403, 404, 503}:
            raise RuntimeError("Непредусмотренный ответ обработчика")
        data = result.read(32769)
        if len(data) > 32768:
            raise RuntimeError("Слишком большой ответ обработчика")
        return result.status, data
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    server: "PortalServer"

    def log_message(self, format: str, *args) -> None:
        # Не выводить секреты и содержимое запросов в журнал systemd.
        pass

    def reply(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def permitted(self, *, admin: bool) -> bool:
        expected = "admin" if admin else "home"
        if self.headers.get("X-Portal-Verified", "") != expected:
            self.reply(403, {"error": "Доступ запрещён"})
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/api/apps":
            if not self.permitted(admin=False):
                return
            path = CATALOG / "apps.json"
            try:
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("Каталог приложений отсутствует")
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
                    raise RuntimeError("Повреждён каталог приложений")
                self.reply(200, data)
            except (OSError, ValueError, RuntimeError):
                self.reply(503, {"error": "Список приложений временно недоступен"})
            return
        if self.path in {"/api/admin/access", "/api/admin/guests"}:
            if not self.permitted(admin=True):
                return
            if self.path == "/api/admin/access":
                self.reply(200, {"allowed": True})
                return
            try:
                catalog = CATALOG / "guests.json"
                if catalog.is_symlink() or not catalog.is_file():
                    raise RuntimeError("Нет списка гостей")
                data = json.loads(catalog.read_text(encoding="utf-8"))
                guests = data.get("guests")
                if not isinstance(guests, list):
                    raise RuntimeError("Неправильный каталог гостей")
                self.reply(200, {"guests": [
                    {"vmid": item.get("vmid"), "name": item.get("name")}
                    for item in guests if isinstance(item, dict)
                ]})
            except (OSError, RuntimeError, ValueError):
                self.reply(503, {"error": "Список гостей временно недоступен"})
            return
        mapping = {
            "/api/admin/users": "/management/users",
            "/api/admin/routes": "/management/routes",
        }
        target = mapping.get(self.path)
        if target is None:
            self.reply(404, {"error": "Не найдено"})
            return
        if not self.permitted(admin=True):
            return
        try:
            status, raw = upstream(target, "GET")
            self.reply(status, json.loads(raw))
        except (OSError, ValueError, RuntimeError):
            self.reply(503, {"error": "Управление временно недоступно"})

    def do_POST(self) -> None:
        mapping = {
            "/api/admin/users": "/management/action",
            "/api/admin/routes": "/management/route-action",
        }
        target = mapping.get(self.path)
        if target is None:
            self.reply(404, {"error": "Не найдено"})
            return
        if not self.permitted(admin=True):
            return
        if (
            self.headers.get("Origin") != ORIGIN
            or self.headers.get("X-Requested-With") != "web-gateway"
            or self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json"
        ):
            self.reply(403, {"error": "Непроверенный источник запроса"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 2 <= length <= MAX_BODY:
                raise ValueError("Недопустимый размер запроса")
            payload = self.rfile.read(length)
            if not isinstance(json.loads(payload), dict):
                raise ValueError("Требуется объект JSON")
            status, raw = upstream(target, "POST", payload)
            self.reply(status, json.loads(raw))
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "Неправильный запрос"})
        except (OSError, RuntimeError):
            self.reply(503, {"error": "Не удалось выполнить действие"})


class PortalServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def server_bind(self) -> None:
        super().server_bind()
        os.chmod(self.server_address, 0o660)


def serve(path: Path = SOCKET) -> None:
    if not os.path.ismount(ROOT):
        raise RuntimeError("Отсутствует постоянный том 140")
    if path.is_symlink():
        raise RuntimeError("Подмена сокета символической ссылкой")
    path.unlink(missing_ok=True)
    try:
        with PortalServer(str(path), Handler) as server:
            server.serve_forever(poll_interval=0.5)
    finally:
        path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Служба запускается только от root")
    serve()


if __name__ == "__main__":
    main()
