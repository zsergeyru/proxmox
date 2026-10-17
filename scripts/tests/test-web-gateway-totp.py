#!/usr/bin/env python3
"""Проверка одноразовой страницы, сохранения ключа и действий администратора."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import os
import sqlite3
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "automation/ansible/roles/web_gateway/files"
URI = ("otpauth://totp/zsergey.ru:admin?algorithm=SHA1&digits=6&"
       "issuer=zsergey.ru&period=30&secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")


def module(name: str, filename: str):
    """Загрузить установленную программу без запуска её точки входа."""
    spec = importlib.util.spec_from_file_location(name, FILES / filename)
    assert spec is not None and spec.loader is not None
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def otp(now: float) -> str:
    key = base64.b32decode("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    digest = hmac.new(key, struct.pack(">Q", int(now // 30)), hashlib.sha1).digest()
    position = digest[-1] & 15
    value = struct.unpack(">I", digest[position:position + 4])[0] & 0x7fffffff
    return str(value % 1_000_000).zfill(6)


def request(server, socket_path: Path, method: str, path: str, body: str = "") -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    data = body.encode()
    client.sendall((f"{method} {path} HTTP/1.1\r\nHost: auth.zsergey.ru\r\n"
                    f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
    server.handle_request()
    result = bytearray()
    while chunk := client.recv(65536):
        result.extend(chunk)
    client.close()
    return bytes(result)


def test() -> None:
    setup = module("gateway_totp_initialize", "initialize-totp.py")
    server_code = module("gateway_totp_server", "totp-setup-server.py")
    users_code = module("gateway_totp_users", "totp-users.py")
    assert server_code.totp_matches(URI, "287082", now=59)
    assert not server_code.totp_matches(URI, "000000", now=59)
    assert not server_code.totp_matches(URI, "1234567", now=59)

    with TemporaryDirectory() as root:
        folder = Path(root)
        png = folder / "admin.png"
        def generated(argv, **kwargs):
            assert argv[:3] == ["docker", "exec", "web-gateway-authelia"]
            assert "generate" in argv and "--force" not in argv
            assert kwargs.get("capture_output")
            png.write_bytes(b"private-png")
            return SimpleNamespace(returncode=0, stdout=("generated URI '" + URI + "'").encode(), stderr=b"")
        with patch.object(setup.subprocess, "run", side_effect=generated) as command:
            setup.initialize("web-gateway-authelia", "admin", "zsergey.ru",
                             png, "/data/totp-setup/admin.png")
            assert command.call_count == 1
            assert png.stat().st_mode & 0o777 == 0o600
            assert (folder / "admin.uri").stat().st_mode & 0o777 == 0o600
            assert URI in (folder / "admin.uri").read_text()
            setup.initialize("web-gateway-authelia", "admin", "zsergey.ru",
                             png, "/data/totp-setup/admin.png")
            assert command.call_count == 1, "Повторный deploy не должен менять TOTP"

        html = folder / "page.html"
        html.write_text("<html>QR</html>")
        socket_path = folder / "test.sock"
        with server_code.SetupServer(socket_path, html, png,
                                     folder / "admin.uri", folder / "admin.confirmed") as server:
            server.timeout = 1
            assert b"200 OK" in request(server, socket_path, "GET", "/totp-setup")
            assert b"private-png" in request(server, socket_path, "GET", "/totp-setup/qr.png")
            assert b"400 Bad Request" in request(server, socket_path, "POST",
                                                "/totp-setup/confirm", "code=000000")
            assert png.exists(), "Неверный код не должен удалять QR"
            assert b"200 OK" in request(server, socket_path, "POST",
                                       "/totp-setup/confirm", "code=" + otp(time.time()))
            assert not png.exists() and not (folder / "admin.uri").exists()
            assert (folder / "admin.confirmed").exists()
        result = setup.initialize("web-gateway-authelia", "admin", "zsergey.ru",
                                  png, "/data/totp-setup/admin.png")
        assert "подтверждён" in result and not png.exists()

    with TemporaryDirectory() as root:
        directory = Path(root)
        users = directory / "users_database.yml"
        users.write_text(yaml.safe_dump({"users": {
            "anna": {"email": "anna@example.com", "password": "hash"},
            "ivan": {"email": "ivan@example.com", "password": "hash"},
        }}), encoding="utf-8")
        notice = directory / "notifications.txt"
        notice.write_text("Date: now\nRecipient: Anna <anna@example.com>\n"
                          "Subject: Confirm your identity\n"
                          "The one-time code to confirm identity:\n"
                          "----------------------------------------\n"
                          "PRQYUNRB\n"
                          "----------------------------------------\n", encoding="utf-8")
        assert users_code.latest_code("anna", users, notice) == "PRQYUNRB"
        notice.write_text(notice.read_text().replace("PRQYUNRB", "12345678"))
        assert users_code.latest_code("anna", users, notice) == "12345678"
        os.utime(notice, (time.time() - 301, time.time() - 301))
        try:
            users_code.latest_code("anna", users, notice)
        except RuntimeError as exc:
            assert "устарел" in str(exc)
        else:
            raise AssertionError("Нельзя выдавать код старше пяти минут")
        os.utime(notice, None)
        try:
            users_code.latest_code("ivan", users, notice)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Код другого пользователя нельзя показывать")
        # Статус читается без доступа к секрету второго фактора.
        database = directory / "status.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE totp_configurations (username TEXT, secret BLOB)")
            connection.execute("CREATE TABLE webauthn_devices (username TEXT)")
            connection.execute("INSERT INTO totp_configurations VALUES ('anna', X'010203')")
            connection.execute("INSERT INTO webauthn_devices VALUES ('ivan')")
        statuses = users_code.factor_statuses(database)
        assert statuses["anna"]["totp"] is True and statuses["anna"]["webauthn"] is False
        assert statuses["ivan"]["totp"] is False and statuses["ivan"]["webauthn"] is True
        assert users_code.available_code("anna") is None or isinstance(users_code.available_code("anna"), dict)
        with patch.object(users_code.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as command:
            users_code.reset_totp("anna", users)
            assert command.call_count == 1
            args = command.call_args.args[0]
            assert args[:3] == ["docker", "exec", "web-gateway-authelia"]
            assert args[3:7] == ["authelia", "storage", "user", "totp"]
            assert "delete" in args and "anna" in args
        # При отсутствии факторов удалять нечего, CLI не вызывается.
        database = directory / "db.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE totp_configurations (username TEXT)")
            connection.execute("CREATE TABLE webauthn_devices (username TEXT)")
        with patch.object(users_code.subprocess, "run") as command:
            users_code.revoke_for_deletion("anna", users)
            command.assert_not_called()
        # Если есть лишь TOTP, не пытаться удалять несуществующий WebAuthn.
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO totp_configurations VALUES ('anna')")
        def delete_registered(argv, **kwargs):
            with sqlite3.connect(database) as connection:
                if argv[6] == "totp":
                    connection.execute("DELETE FROM totp_configurations WHERE username = 'anna'")
                elif argv[6] == "webauthn":
                    connection.execute("DELETE FROM webauthn_devices WHERE username = 'anna'")
                else:
                    raise AssertionError("Неожиданный второй фактор")
            return SimpleNamespace(returncode=0, stderr=b"")
        with patch.object(users_code.subprocess, "run", side_effect=delete_registered) as command:
            users_code.revoke_for_deletion("anna", users)
            assert command.call_count == 1
            assert "totp" in command.call_args.args[0]
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO totp_configurations VALUES ('anna')")
            connection.execute("INSERT INTO webauthn_devices VALUES ('anna')")
        with patch.object(users_code.subprocess, "run", side_effect=delete_registered) as command:
            users_code.revoke_for_deletion("anna", users)
            assert command.call_count == 2
        # Ошибка удаления не должна приводить к ложному успеху.
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO totp_configurations VALUES ('anna')")
        with patch.object(users_code.subprocess, "run",
                          return_value=SimpleNamespace(returncode=1, stderr=b"unexpected error")):
            try:
                users_code.revoke_for_deletion("anna", users)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Ошибка удаления должна остановить операцию")
        assert users_code.factor_statuses(database)["anna"]["totp"] is True
        # Повреждённая SQLite-база не должна скрываться как отсутствие TOTP.
        damaged = directory / "broken.sqlite3"
        damaged.write_bytes(b"not sqlite")
        try:
            users_code.factor_statuses(damaged)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Повреждённая база обязана возвращать контролируемую ошибку")
        assert "ivan" in users.read_text(), "Сброс TOTP не изменяет пользователей"
        records = yaml.safe_load(users.read_text())
        records["users"]["admin"] = {"email": "admin@example.com", "password": "hash"}
        users.write_text(yaml.safe_dump(records), encoding="utf-8")
        setup_dir = directory / "totp-setup"
        setup_dir.mkdir()
        for name in ("admin.confirmed", "admin.png", "admin.uri"):
            (setup_dir / name).write_text("old")
        with (patch.object(users_code, "STATE", directory),
              patch.object(users_code.subprocess, "run",
                           return_value=SimpleNamespace(returncode=0))):
            users_code.reset_totp("admin", users)
        assert not any(setup_dir.iterdir()), "Явный сброс admin должен разрешить новую регистрацию"
    print("140: QR ограничен, подтверждается кодом и удаляется без сброса TOTP")


if __name__ == "__main__":
    test()
