#!/usr/bin/env python3
"""Закрытое управление 140: права, пароль, доступ и безопасность."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "automation/ansible/roles/web_gateway/files"
WIDGET = ROOT / "infrastructure/guests/140-web-gateway/widgets/authelia-users.json"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, FILES / file)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def query(server, path, method="GET", data=None, auth="secret-token-for-testing"):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(server.server_address))
    raw = json.dumps(data or {}).encode("utf-8")
    header = (f"{method} {path} HTTP/1.1\r\nHost: caddy\r\n"
              f"Content-Length: {len(raw)}\r\nAuthorization: Bearer {auth}\r\n"
              "Connection: close\r\n\r\n").encode("utf-8")
    client.sendall(header + raw)
    server.handle_request()
    payload = bytearray()
    while chunk := client.recv(8192):
        payload.extend(chunk)
    client.close()
    status = int(payload.split(b" ")[1])
    body = json.loads(payload.partition(b"\r\n\r\n")[2])
    return status, body


def test():
    api = load("admin_api_140", "admin-api.py")
    users = load("users_140", "user-accounts.py")
    totp = load("totp_140", "totp-users.py")
    definition = json.loads(WIDGET.read_text(encoding="utf-8"))
    assert definition["sources"]["default"]["networkScope"] == "private"
    assert definition["sources"]["default"]["auth"] == "bearer"
    assert definition["requests"]["operate"]["permission"] == "full"
    routes_widget = json.loads((
        ROOT / "infrastructure/guests/140-web-gateway/widgets/caddy-routes.json"
    ).read_text(encoding="utf-8"))
    assert routes_widget["requests"]["update"]["permission"] == "full"
    assert routes_widget["requests"]["update"]["kind"] == "action"
    assert "secret-token-for-testing" not in WIDGET.read_text()
    assert not any("/var/run/docker.sock" in str(item) for item in definition.values())

    with TemporaryDirectory() as root:
        state = Path(root)
        (state / "authelia").mkdir()
        (state / "secrets").mkdir()
        (state / "management/admin-socket").mkdir(parents=True)
        (state / "secrets/.initialize.lock").write_text("lock")
        file = state / "authelia/users_database.yml"
        file.write_text(yaml.safe_dump({"users": {"admin": {
            "email": "admin@example.org", "password": "hash", "groups": ["admins"],
        }}}), encoding="utf-8")
        file.chmod(0o600)
        (state / "management/routes.yaml").write_text(yaml.safe_dump([
            {"domain": "home.zsergey.ru", "service": "portal",
             "access": "public", "enabled": True},
            {"domain": "photos.zsergey.ru", "service": "immich",
             "access": "app_auth", "enabled": False},
        ]), encoding="utf-8")
        token = state / "secrets/administration-token"
        with patch.object(api, "STATE", state), patch.object(api.os.path, "ismount", return_value=True):
            assert api.initialize_token(token) is True
            previous = token.read_text()
            assert api.initialize_token(token) is False
            assert token.read_text() == previous
            assert token.stat().st_mode & 0o777 == 0o600

        sock = state / "management/admin-socket/control.sock"
        with patch.object(api, "STATE", state), patch.object(users.os.path, "ismount", return_value=True):
            route_action = Mock()
            with api.ControlServer(
                sock, "secret-token-for-testing", users, totp,
                SimpleNamespace(change_route=route_action),
            ) as server:
                server.timeout = 1
                assert query(server, "/management/users", auth="wrong")[0] == 403
                assert query(server, "/management/users", auth="")[0] == 403
                code, body = query(server, "/management/users")
                assert code == 200 and len(body["users"]) == 1
                assert "password" not in body["users"][0]
                code, body = query(server, "/management/routes")
                assert code == 200 and len(body["routes"]) == 2
                assert body["routes"][1]["enabled"] is False
                code, _ = query(server, "/management/action", "POST",
                                {"action": "disable", "username": "admin", "confirm": True})
                assert code == 400
                code, body = query(server, "/management/action", "POST", {
                    "action": "add", "username": "anna", "email": "anna@example.org",
                    "name": "Анна", "groups": "family", "confirm": False,
                })
                assert code == 200 and body["password"]
                assert "password" not in query(server, "/management/users")[1]["users"][1]
                code, body = query(server, "/management/action", "POST",
                                   {"action": "password", "username": "anna", "confirm": False})
                assert code == 400 and "error" in body
                code, body = query(server, "/management/action", "POST",
                                   {"action": "password", "username": "anna", "confirm": True})
                assert code == 200 and body["password"]
                assert query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": True, "confirm": False,
                })[0] == 400
                assert query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": "true", "confirm": True,
                })[0] == 400
                assert query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": True, "confirm": True,
                }, auth="incorrect")[0] == 403
                assert route_action.call_count == 0
                code, body = query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": True, "confirm": True,
                })
                assert code == 200 and body["status"] == "ok"
                assert route_action.call_count == 1
                assert route_action.call_args.args[2:] == ("ai.zsergey.ru", True, "private")
                assert query(server, "/unknown")[0] == 404
        assert file.stat().st_mode & 0o777 == 0o600
        assert yaml.safe_load(file.read_text())["users"]["admin"]["password"] == "hash"
    print("140: закрытый интерфейс, права и защита паролей проверены")


if __name__ == "__main__":
    test()
