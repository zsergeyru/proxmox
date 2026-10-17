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
        policy_file = state / "management/routes-policy.json"
        policy_file.write_text(json.dumps({"web_gateway": {"caddy": {
            "routes": [{"domain": "home.zsergey.ru"}, {"domain": "photos.zsergey.ru"}]
        }}}), encoding="utf-8")
        token = state / "secrets/administration-token"
        with patch.object(api, "STATE", state), patch.object(api.os.path, "ismount", return_value=True):
            assert api.initialize_token(token) is True
            previous = token.read_text()
            assert api.initialize_token(token) is False
            assert token.read_text() == previous
            assert token.stat().st_mode & 0o777 == 0o600

        sock = state / "management/admin-socket/control.sock"
        with (patch.object(api, "STATE", state),
              patch.object(api, "ROUTE_POLICY", policy_file),
              patch.object(users.os.path, "ismount", return_value=True)):
            route_action = Mock()
            with api.ControlServer(
                sock, "secret-token-for-testing", users, totp,
                SimpleNamespace(change_route=route_action, apply_route=route_action),
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
                code, body = query(server, "/management/action", "POST", {
                    "action": "edit", "username": "anna", "name": "Анна новая",
                    "groups": "family,guests", "confirm": False,
                })
                assert code == 200
                people = yaml.safe_load(file.read_text())["users"]
                assert people["anna"]["displayname"] == "Анна новая"
                assert people["anna"]["groups"] == ["family", "guests"]
                code, body = query(server, "/management/action", "POST",
                                   {"action": "password", "username": "anna", "confirm": False})
                assert code == 400 and "error" in body
                code, body = query(server, "/management/action", "POST",
                                   {"action": "password", "username": "anna", "confirm": True})
                assert code == 200 and body["password"]
                # Удаление должно подтверждаться и очищать второй фактор до файла.
                code, _ = query(server, "/management/action", "POST", {
                    "action": "delete", "username": "anna", "confirm": False,
                })
                assert code == 400
                with patch.object(totp, "revoke_for_deletion", return_value=None) as revoke:
                    code, body = query(server, "/management/action", "POST", {
                        "action": "delete", "username": "anna", "confirm": True,
                    })
                    assert code == 200 and body["status"] == "ok"
                    revoke.assert_called_once_with("anna")
                assert "anna" not in yaml.safe_load(file.read_text())["users"]
                assert query(server, "/management/action", "POST", {
                    "action": "delete", "username": "admin", "confirm": True,
                })[0] == 400
                assert query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": True, "confirm": False,
                })[0] == 200
                assert query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": "true", "confirm": True,
                })[0] == 400
                assert query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": True, "confirm": True,
                }, auth="incorrect")[0] == 403
                assert route_action.call_count == 1
                code, body = query(server, "/management/route-action", "POST", {
                    "domain": "ai.zsergey.ru", "access": "private",
                    "enabled": True, "confirm": True,
                })
                assert code == 200 and body["status"] == "ok"
                assert route_action.call_count == 2
                assert route_action.call_args.args[2:] == ("update", "ai.zsergey.ru")
                assert route_action.call_args.kwargs["enabled"] is True
                assert route_action.call_args.kwargs["access"] == "private"
                for action in ("create", "update", "delete"):
                    payload = {"action": action, "domain": "printer.zsergey.ru"}
                    if action == "create":
                        payload.update(name="Принтер", upstream="http://192.168.1.50:7125",
                                       enabled=False, access="protected")
                    if action == "update":
                        payload.update(enabled=True, access="private")
                    if action == "delete":
                        assert query(server, "/management/route-action", "POST", payload)[0] == 400
                        payload["confirm"] = True
                    assert query(server, "/management/route-action", "POST", payload)[0] == 200
                assert query(server, "/unknown")[0] == 404
        assert file.stat().st_mode & 0o777 == 0o600
        assert yaml.safe_load(file.read_text())["users"]["admin"]["password"] == "hash"
    print("140: закрытый интерфейс, права и защита паролей проверены")


if __name__ == "__main__":
    test()
