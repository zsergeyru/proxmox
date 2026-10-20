"""Административная панель infra-manager: HTTPS, OpenBao, закрытая сессия.

Пароли проверяются OpenBao userpass и не хранятся в контейнере.
Команды передаются в проверяемый системный посредник по Unix-сокету.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app import _history, PHASES

WEB_HOST = os.environ.get("INFRA_WEB_PUBLIC_HOST", "192.168.9.10:8443")
CERT = os.environ.get("INFRA_WEB_CERT", "/etc/infra-manager/web/tls/server.crt")
KEY = os.environ.get("INFRA_WEB_KEY", "/etc/infra-manager/web/tls/server.key")
SOCKET = os.environ.get("INFRA_WEB_SOCKET", "/run/infra-web/operations.sock")
OPENBAO = "http://127.0.0.1:8200"
COOKIE = "infra_session"
SESSION_TTL = 3600
SESSION_LIMIT = 60
MAX_BODY = 8192
REQ_ID = re.compile(r"^[0-9a-f]{32}$")
sessions: dict[str, dict] = {}
attempts: dict[str, list[float]] = {}
guard = threading.RLock()


def _authenticate(username: str, password: str) -> bool:
    """Проверять пароль человека через OpenBao, не доверяя присланному имени."""
    if username != "operator" or not password or len(password) > 2048:
        return False
    payload = json.dumps({"password": password}).encode()
    request = urllib.request.Request(
        OPENBAO + "/v1/auth/userpass/login/operator",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            result = json.load(reply)
        token = result.get("auth", {}).get("client_token")
        if not isinstance(token, str) or not token:
            return False
        # Не хранить токен в сессии. Отозвать кратковременный входной токен.
        revoke = urllib.request.Request(
            OPENBAO + "/v1/auth/token/revoke-self",
            data=b"{}", headers={"X-Vault-Token": token,
                                  "Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(revoke, timeout=5).close()
        except (OSError, urllib.error.HTTPError):
            # Отказ отзыва не превращает OpenBao token в полномочия веб-панели.
            pass
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _session(cookie: str | None) -> tuple[str, dict] | None:
    if not cookie or len(cookie) > 1024:
        return None
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except Exception:
        return None
    if COOKIE not in parsed:
        return None
    token = parsed[COOKIE].value
    with guard:
        session = sessions.get(token)
        if session and session["expires"] > time.monotonic():
            return token, session
        sessions.pop(token, None)
    return None


def _new_session() -> tuple[str, dict]:
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    session = {"csrf": secrets.token_urlsafe(32), "expires": now + SESSION_TTL}
    with guard:
        for key, value in tuple(sessions.items()):
            if value["expires"] <= now:
                sessions.pop(key, None)
        if len(sessions) >= SESSION_LIMIT:
            raise ValueError("Превышен предел числа активных входов")
        sessions[token] = session
    return token, session


def _allow_attempt(ip: str) -> bool:
    with guard:
        now = time.monotonic()
        timestamps = [x for x in attempts.get(ip, []) if now - x < 600]
        if len(timestamps) >= 5:
            attempts[ip] = timestamps
            return False
        timestamps.append(now)
        attempts[ip] = timestamps
        return True


def _send_action(kind: str, vmid: int, request_id: str) -> dict:
    if kind not in {"status", "sync", "repair", "deploy"} or (
        not isinstance(vmid, int) or not 1 <= vmid <= 999999
    ) or not REQ_ID.fullmatch(request_id):
        raise ValueError("Недопустимая операция")
    payload = json.dumps(
        {"kind": kind, "vmid": vmid, "request_id": request_id}
    ).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(SOCKET)
        connection.sendall(payload)
        output = bytearray()
        while not output.endswith(b"\n") and len(output) <= 2048:
            block = connection.recv(2049 - len(output))
            if not block:
                break
            output.extend(block)
    if len(output) > 2048 or not output.endswith(b"\n"):
        raise ValueError("Посредник не подтвердил операцию")
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("accepted") is not True:
        raise ValueError("Посредник отклонил операцию")
    if result.get("id") != request_id:
        raise ValueError("Ответ относится к другому заданию")
    return result


def _layout(inner: str) -> bytes:
    return ("""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>infra-manager</title><style>
:root{color-scheme:dark}body{font-family:system-ui;background:#0c1525;
color:#eaf0f7;max-width:1050px;margin:3rem auto;padding:0 1rem}
main{background:#142237;padding:1.5rem;border-radius:12px}
table{width:100%;border-collapse:collapse}td,th{padding:.55rem;
border-bottom:1px solid #345;text-align:left}input,select,button{
padding:.55rem;margin:.25rem;border-radius:6px}
a{color:#9bded8}code{color:#96d1f1}
</style></head><body><h1>Управление инфраструктурой</h1>
<main>""" + inner + "</main></body></html>").encode()


def _dashboard(csrf: str, notice: str = "") -> bytes:
    records = _history()
    rows = []
    for record in records:
        operation_id = str(record.get("id", ""))
        if not REQ_ID.fullmatch(operation_id):
            continue
        values = (operation_id[:8], str(record.get("target", "")),
                  str(record.get("kind", "")),
                  PHASES.get(record.get("phase"), "Неизвестно"),
                  str(record.get("created_at", "")))
        rows.append("<tr>" + "".join(
            "<td>" + html.escape(value) + "</td>" for value in values
        ) + "</tr>")
    table = "".join(rows) or '<tr><td colspan="5">Заданий пока нет</td></tr>'
    token = html.escape(csrf, quote=True)
    form = (
        f'<p>{html.escape(notice)}</p><form method="POST" action="/action">'
        f'<input type="hidden" name="csrf" value="{token}">'
        '<label>Гость (VMID) <input name="vmid" type="number" min="1" required></label>'
        '<label>Действие <select name="kind">'
        '<option value="status">Проверить</option>'
        '<option value="sync">Синхронизировать</option>'
        '<option value="repair">Исправить</option>'
        '<option value="deploy">Развернуть</option>'
        '</select></label><button type="submit">Выполнить</button></form>'
        '<p>Изменяйте только известные гости. Система повторно проверяет права '
        'и записывает результат задания независимо от браузера.</p>'
        '<table><tr><th>ID</th><th>VMID</th><th>Действие</th>'
        '<th>Состояние</th><th>Время UTC</th></tr>' + table + '</table>'
        f'<form method="POST" action="/logout"><input type="hidden" '
        f'name="csrf" value="{token}"><button>Выйти</button></form>'
    )
    return _layout(form)


def _login() -> bytes:
    return _layout(
        "<h2>Вход через OpenBao</h2>"
        '<form method="POST" action="/login">'
        '<label>Логин <input name="username" autocomplete="username" required></label>'
        '<label>Пароль <input type="password" name="password" '
        'autocomplete="current-password" required></label>'
        '<button>Войти</button></form>'
    )


class Handler(BaseHTTPRequestHandler):
    """Только известные маршруты, короткие тела запросов и закрытая сессия."""

    def log_message(self, fmt: str, *args) -> None:
        # URL запроса и данные пользователя не записываются в журнал контейнера.
        return

    def _reply(
        self, status: HTTPStatus, body: bytes, *,
        content_type: str = "text/html; charset=utf-8", cookie: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", "max-age=3600")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
        )
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> tuple[str, dict] | None:
        return _session(self.headers.get("Cookie"))

    def _same_origin(self) -> bool:
        return (
            self.headers.get("Host") == WEB_HOST
            and self.headers.get("Origin") == "https://" + WEB_HOST
        )

    def _form(self) -> dict[str, str]:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise ValueError("Неизвестный тип запроса")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("Неверный размер запроса")
        parsed = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError("Повторный параметр")
        return {key: value[0] for key, value in parsed.items()}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._reply(
                HTTPStatus.OK, b"ok\n",
                content_type="text/plain; charset=utf-8",
            )
            return
        session = self._authorized()
        if path == "/" or path == "/login":
            self._reply(
                HTTPStatus.OK,
                _dashboard(session[1]["csrf"]) if session else _login(),
            )
        elif path == "/api/operations":
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            data = json.dumps(_history(), ensure_ascii=False).encode()
            self._reply(
                HTTPStatus.OK, data, content_type="application/json; charset=utf-8"
            )
        else:
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._same_origin():
            self._reply(HTTPStatus.FORBIDDEN, b"origin rejected\n")
            return
        try:
            form = self._form()
        except (ValueError, UnicodeError):
            self._reply(HTTPStatus.BAD_REQUEST, b"invalid form\n")
            return
        if path == "/login":
            ip = self.client_address[0]
            if not _allow_attempt(ip):
                self._reply(HTTPStatus.TOO_MANY_REQUESTS, b"attempt limit\n")
                return
            if not _authenticate(form.get("username", ""), form.get("password", "")):
                self._reply(HTTPStatus.UNAUTHORIZED, _login())
                return
            with guard:
                attempts.pop(ip, None)
            try:
                token, session = _new_session()
            except ValueError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"session limit\n")
                return
            self._reply(
                HTTPStatus.OK, _dashboard(session["csrf"]),
                cookie=f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/",
            )
            return
        auth = self._authorized()
        if not auth:
            self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
            return
        token, session = auth
        if not secrets.compare_digest(form.get("csrf", ""), session["csrf"]):
            self._reply(HTTPStatus.FORBIDDEN, b"csrf rejected\n")
            return
        if path == "/logout":
            with guard:
                sessions.pop(token, None)
            self._reply(
                HTTPStatus.OK, _login(),
                cookie=f"{COOKIE}=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/",
            )
            return
        if path != "/action":
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n")
            return
        try:
            kind = form.get("kind", "")
            vmid = int(form.get("vmid", ""))
            request_id = form.get("request_id") or secrets.token_hex(16)
            result = _send_action(kind, vmid, request_id)
        except (ValueError, OSError, TimeoutError):
            self._reply(
                HTTPStatus.BAD_REQUEST, _dashboard(session["csrf"], "Задание не принято")
            )
            return
        self._reply(
            HTTPStatus.ACCEPTED,
            _dashboard(session["csrf"], "Задание принято: " + result["id"]),
        )


def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(CERT, KEY)
    with ThreadingHTTPServer(("0.0.0.0", 8443), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
