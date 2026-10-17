#!/usr/bin/env python3
"""Закрытое управление пользователями и маршрутами домашнего портала 140.

Доступ только по локальному Unix-сокету с обязательным служебным ключом.
"""
from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import secrets
import socketserver

STATE = Path("/var/lib/web-gateway")
TOKEN_FILE = STATE / "secrets/administration-token"
SOCKET = STATE / "management/admin-socket/control.sock"
CODE_ROOT = Path("/usr/local/lib/web-gateway")


def import_file(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, CODE_ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError("Не найден проверенный сценарий управления")
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def initialize_token(path: Path = TOKEN_FILE) -> bool:
    if not os.path.ismount(STATE):
        raise RuntimeError("Постоянное хранилище не подключено")
    if path.is_symlink():
        raise RuntimeError("Недопустимая ссылка вместо ключа доступа")
    if path.exists():
        if not path.is_file() or path.stat().st_size < 40 or path.stat().st_mode & 0o077:
            raise RuntimeError("Сохранённый ключ доступа повреждён")
        return False
    secret = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(secret + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def response_code_for(exc: Exception) -> int:
    return 400 if isinstance(exc, ValueError) else 503


class Handler(BaseHTTPRequestHandler):
    server: "ControlServer"

    def log_message(self, fmt: str, *args) -> None:
        # Не записывать учётные данные, пароли и проверочные коды в журнал.
        pass

    def reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def authorised(self) -> bool:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            self.reply(HTTPStatus.UNAUTHORIZED, {"error": "Доступ запрещён"})
            return False
        expected = self.server.token
        if not hmac.compare_digest(value[7:], expected):
            self.reply(HTTPStatus.FORBIDDEN, {"error": "Доступ запрещён"})
            return False
        return True

    def do_GET(self) -> None:
        if not self.authorised():
            return
        if self.path == "/management/routes":
            try:
                self.server.accounts.require_state(STATE)
                import yaml
                route_file = STATE / "management/routes.yaml"
                if route_file.is_symlink() or not route_file.is_file():
                    raise RuntimeError("Сохранённые маршруты недоступны")
                routes = yaml.safe_load(route_file.read_text(encoding="utf-8"))
                if not isinstance(routes, list):
                    raise RuntimeError("Сохранённые маршруты повреждены")
                items = []
                for route in routes:
                    if not isinstance(route, dict):
                        raise RuntimeError("Неверный формат маршрута")
                    items.append({
                        "domain": route.get("domain", ""),
                        "service": route.get("service", ""),
                        "upstream": route.get("upstream", ""),
                        "access": route.get("access", "protected"),
                        "enabled": route.get("enabled", False) is True,
                    })
                self.reply(200, {"routes": items})
            except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
                self.reply(503, {"error": str(exc)})
        elif self.path == "/management/users":
            try:
                module = self.server.accounts
                module.require_state(STATE)
                entries = module.read_users(STATE / "authelia/users_database.yml")["users"]
                rows = [
                    {"username": username, "displayname": item.get("displayname", username),
                     "email": item.get("email", ""), "groups": item.get("groups", []),
                     "disabled": item.get("disabled") is True}
                    for username, item in sorted(entries.items())
                ]
                self.reply(200, {"users": rows})
            except (OSError, RuntimeError, ValueError) as exc:
                self.reply(503, {"error": str(exc)})
        else:
            self.reply(404, {"error": "Не найдено"})

    def do_POST(self) -> None:
        if not self.authorised():
            return
        if self.path == "/management/route-action":
            try:
                if self.server.routes is None:
                    raise RuntimeError("Управление доменами ещё не подготовлено")
                length = int(self.headers.get("Content-Length", "-1"))
                if not 2 <= length <= 1024:
                    raise ValueError("Неверный размер запроса")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {
                    "domain", "enabled", "access", "confirm"
                }:
                    raise ValueError("Необходимо указать домен, доступ и подтверждение")
                if body["confirm"] is not True:
                    raise ValueError("Подтвердите изменение домена")
                if not isinstance(body["domain"], str) or type(body["enabled"]) is not bool:
                    raise ValueError("Неверный домен или состояние")
                if not isinstance(body["access"], str):
                    raise ValueError("Неверный режим доступа")
                self.server.routes.change_route(
                    STATE, Path("/etc/web-gateway/caddy"),
                    body["domain"], body["enabled"], body["access"],
                )
                self.reply(200, {"status": "ok", "domain": body["domain"]})
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                self.reply(response_code_for(exc), {"error": str(exc)})
            return
        if self.path != "/management/action":
            self.reply(404, {"error": "Не найдено"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 2 or length > 4096:
                raise ValueError("Неверный размер запроса")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) - {
                "action", "username", "email", "name", "groups", "confirm"
            }:
                raise ValueError("Неожиданные поля")
            action = body.get("action")
            username = body.get("username")
            if not isinstance(action, str) or not isinstance(username, str):
                raise ValueError("Укажите действие и пользователя")
            if any(not isinstance(body.get(field, ""), str) for field in ("email", "name", "groups")):
                raise ValueError("Неверные значения полей")
            if body.get("confirm", False) not in (True, False):
                raise ValueError("Подтверждение должно быть логическим значением")
            self.server.accounts.require_state(STATE)
            sensitive = {"password", "disable", "delete", "reset_totp"}
            if action in sensitive and body.get("confirm") is not True:
                raise ValueError("Подтвердите действие")
            if action in {"add", "password", "groups", "edit", "disable", "enable", "delete"}:
                if action == "password" and username == "admin":
                    if self.server.admin_reset is None:
                        raise RuntimeError("Сброс пароля администратора не подготовлен")
                    password = self.server.admin_reset.reset_admin_password(STATE)
                else:
                    password = self.server.accounts.change(
                        STATE, action, username,
                        email=body.get("email", ""), displayname=body.get("name", ""),
                        groups=body.get("groups", ""),
                        revoke_totp=self.server.totp.reset_totp if action == "delete" else None,
                    )
                answer = {"status": "ok", "username": username}
                if password:
                    answer["password"] = password
            elif action == "reset_totp":
                self.server.totp.reset_totp(username)
                answer = {"status": "ok", "username": username}
            elif action == "code":
                answer = {"status": "ok", "username": username,
                          "code": self.server.totp.latest_code(username)}
            else:
                raise ValueError("Неизвестное действие")
            self.reply(200, answer)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            self.reply(response_code_for(exc), {"error": str(exc)})


class ControlServer(socketserver.UnixStreamServer):
    def __init__(self, path: Path, token: str, accounts, totp, routes=None, admin_reset=None):
        self.token, self.accounts, self.totp, self.routes = token, accounts, totp, routes
        self.admin_reset = admin_reset
        super().__init__(str(path), Handler)
        os.chmod(path, 0o660)


def run_server() -> None:
    if not os.path.ismount(STATE):
        raise RuntimeError("Постоянный том 140 не подключён")
    if TOKEN_FILE.is_symlink() or SOCKET.is_symlink():
        raise RuntimeError("Недопустимые ссылки в состоянии")
    secret = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if len(secret) < 40:
        raise RuntimeError("Недействительный ключ управления")
    accounts = import_file("user-accounts.py", "accounts_140")
    totp = import_file("totp-users.py", "totp_140")
    routes = import_file("manage-routes.py", "routes_140")
    admin_reset = import_file("reset-admin-password.py", "admin_reset_140")
    SOCKET.unlink(missing_ok=True)
    try:
        with ControlServer(SOCKET, secret, accounts, totp, routes, admin_reset) as server:
            server.serve_forever(poll_interval=0.5)
    finally:
        SOCKET.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "serve"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Требуются права администратора гостя")
    if args.action == "init":
        print("140: ключ управления " + ("создан" if initialize_token() else "сохранён"))
    else:
        run_server()


if __name__ == "__main__":
    main()
