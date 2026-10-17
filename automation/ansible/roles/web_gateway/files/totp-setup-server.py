#!/usr/bin/env python3
"""Временная локальная страница первичной привязки TOTP администратора.

Обслуживается через Unix-сокет Caddy, живёт не более 15 минут.
Никакого дополнительного постоянного сетевого порта не открывает.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import re
import socketserver
import struct
import time
from urllib.parse import parse_qs, urlparse


def totp_matches(uri: str, code: str, now: float | None = None) -> bool:
    """Проверить шестизначный код по параметрам, созданным самой Authelia."""
    if re.fullmatch(r"[0-9]{6}", code) is None:
        return False
    parsed = urlparse(uri.strip())
    if parsed.scheme != "otpauth" or parsed.netloc != "totp":
        raise ValueError("Неверный формат TOTP")
    args = parse_qs(parsed.query)
    secret = args["secret"][0].upper()
    digits = int(args.get("digits", ["6"])[0])
    period = int(args.get("period", ["30"])[0])
    algorithm = args.get("algorithm", ["SHA1"])[0].lower()
    if digits != 6 or not (15 <= period <= 120) or algorithm not in {"sha1", "sha256", "sha512"}:
        raise ValueError("Неподдерживаемые параметры TOTP")
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((time.time() if now is None else now) // period)
    for offset in (-1, 0, 1):
        digest = hmac.new(key, struct.pack(">Q", counter + offset), getattr(hashlib, algorithm)).digest()
        position = digest[-1] & 15
        value = struct.unpack(">I", digest[position:position + 4])[0] & 0x7fffffff
        candidate = str(value % 1_000_000).zfill(6)
        if hmac.compare_digest(candidate, code):
            return True
    return False


class SetupHandler(BaseHTTPRequestHandler):
    """Показывать QR и принимать подтверждение только в активном окне."""

    server: SetupServer

    def log_message(self, fmt: str, *args: object) -> None:
        # Не выводить URL, формы или содержимое ответов в журнал.
        pass

    def respond(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def page(self, message: str, status: int = 200) -> None:
        body = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Google Authenticator</title><body><h1>' + message +
                '</h1><p><a href="https://auth.zsergey.ru">Перейти ко входу</a></p></body></html>')
        self.respond(status, body.encode("utf-8"))

    def active(self) -> bool:
        return time.monotonic() < self.server.deadline and not self.server.finished

    def do_GET(self) -> None:
        if not self.active():
            self.page("Время для подключения закончилось. Если телефон ещё не подключён, повторите развёртывание гостя 140.", 410)
            return
        if self.path in ("/totp-setup", "/totp-setup/"):
            self.respond(200, self.server.html.read_bytes())
        elif self.path == "/totp-setup/qr.png":
            self.respond(200, self.server.png.read_bytes(), "image/png")
        else:
            self.page("Страница не найдена", 404)

    def do_POST(self) -> None:
        if not self.active():
            self.page("Страница подключения закрыта. Если вход с кодом из телефона уже работает, ничего делать не нужно.", 410)
            return
        if self.path not in ("/totp-setup/confirm", "/totp-setup/close"):
            self.page("Страница не найдена", 404)
            return
        if self.path.endswith("/close"):
            self.server.finished = True
            self.page("Страница закрыта без подключения. Чтобы попробовать снова, повторите развёртывание гостя 140.")
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if not 1 <= size <= 128:
            self.page("Некорректный запрос", 400)
            return
        fields = parse_qs(self.rfile.read(size).decode("ascii", "strict"), strict_parsing=True)
        code = fields.get("code", [""])[0].replace(" ", "")
        try:
            ok = totp_matches(self.server.uri.read_text(encoding="utf-8"), code)
        except (ValueError, KeyError, UnicodeError):
            self.page("Не получилось проверить шесть цифр с телефона. Подключение не завершено.", 500)
            self.server.finished = True
            return
        if not ok:
            self.page("Неверный код. Вернитесь назад и попробуйте снова.", 400)
            return
        # Сначала подтверждение на постоянном томе, потом удаление исходного QR.
        fd = os.open(self.server.confirmed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("confirmed\n")
            f.flush()
            os.fsync(f.fileno())
        self.server.png.unlink(missing_ok=True)
        self.server.uri.unlink(missing_ok=True)
        self.server.finished = True
        self.page("Телефон подключён. Рисунок удалён. Теперь для входа используйте код из приложения Google Authenticator.")


class SetupServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, socket_path: Path, html: Path, png: Path, uri: Path, confirmed: Path):
        self.html, self.png, self.uri, self.confirmed = html, png, uri, confirmed
        self.deadline = time.monotonic() + 900
        self.finished = False
        super().__init__(str(socket_path), SetupHandler)
        os.chmod(socket_path, 0o660)


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("socket", "html", "png", "uri", "confirmed"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.confirmed.exists() or not args.png.is_file() or not args.uri.is_file():
        raise RuntimeError("Первичная регистрация отсутствует либо уже завершена")
    if args.socket.is_symlink():
        raise RuntimeError("Неверный путь сокета")
    args.socket.unlink(missing_ok=True)
    try:
        with SetupServer(args.socket, args.html, args.png, args.uri, args.confirmed) as server:
            server.timeout = 0.5
            while time.monotonic() < server.deadline and not server.finished:
                server.handle_request()
    finally:
        args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
