#!/usr/bin/env python3
"""Веб-портал 140: права, локальная сеть, CSRF, миграция и Caddy."""
from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "automation/ansible/roles/web_gateway"
GUEST = ROOT / "infrastructure/guests/140-web-gateway"


def load(name):
    filename = ROLE / "files" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_policy():
    provision = yaml.safe_load((GUEST / "provision.yaml").read_text())["web_gateway"]
    env = Environment(loader=FileSystemLoader(str(ROLE / "templates")), undefined=StrictUndefined)
    env.filters["bool"] = bool
    variables = {"provision": {"web_gateway": provision}, "web_gateway_routes": provision["caddy"]["routes"]}
    compose = yaml.safe_load(env.get_template("docker-compose.yml.j2").render(**variables))
    assert set(compose["services"]) == {"caddy", "authelia"}
    assert "homarr" not in compose["services"]
    assert all("docker.sock" not in str(c) for c in compose["services"].values())
    assert "/run/portal:ro" in str(compose["services"]["caddy"]["volumes"])
    assert "/srv/portal:ro" in str(compose["services"]["caddy"]["volumes"])
    assert "/etc/caddy/portal" not in str(compose["services"]["caddy"]["volumes"])
    assert not any("portal" in str(x) for x in compose["services"]["caddy"]["ports"])
    policy = env.get_template("authelia-configuration.yml.j2").render(**variables)
    p = yaml.safe_load(policy.split("\nidentity_providers:", 1)[0])
    rules = p["access_control"]["rules"]
    assert rules[0]["subject"] == ["group:admins"]
    assert rules[0]["policy"] == "two_factor"
    assert rules[0]["resources"] == [r"^/admin(?:/.*)?$", r"^/api/admin(?:/.*)?$"]
    assert rules[1]["policy"] == "deny" and rules[1]["resources"] == rules[0]["resources"]
    assert rules[1].get("subject") is None
    assert rules[2]["policy"] == "two_factor"
    sites = env.get_template("sites.generated.caddy.j2").render(**variables)
    assert "forward_auth authelia:9091" in sites
    assert "@admin_api_lan" in sites and "@admin_ui_lan" in sites
    assert "remote_ip 192.168.0.0/16" in sites
    assert "X-Portal-Verified admin" in sites and "X-Portal-Verified home" in sites
    assert "uri strip_prefix /static" in sites
    assert "@static path /static/portal.css /static/portal.js" in sites
    assert "header_up -Authorization" in sites
    assert "Content-Security-Policy" in sites and "script-src 'self'" in sites
    assert "rewrite * /portal-admin.html" in sites
    assert "rewrite * /portal-home.html" in sites
    assert "http://homarr:7575" not in sites
    home = (ROLE / "files/portal-home.html").read_text()
    admin = (ROLE / "files/portal-admin.html").read_text()
    js = (ROLE / "files/portal.js").read_text()
    assert '<script defer src="/static/portal.js"></script>' in home
    assert "password" not in home.lower()
    assert 'id="admin-link" hidden' in home
    assert 'id="user-form"' in admin and 'id="route-form"' in admin
    assert 'name="email"' not in admin
    assert 'name="action"' not in admin
    for identifier in ('add-user', 'users-list', 'users-search', 'user-dialog',
                       'confirm-dialog', 'copy-credential', 'credential-value'):
        assert 'id="' + identifier + '"' in admin
    assert 'name="group"' in admin
    assert 'userAction(' in js and 'openUserForm(' in js
    assert 'result.code' in js and 'credential(result.code' in js
    assert 'type="password"' not in admin
    assert "textContent" in js and "innerHTML" not in js
    assert 'Origin' not in js  # браузер отправляет Origin сам
    assert "X-Requested-With" in js
    assert "/api/admin/access" in js
    assert provision["portal"]["administration"]["groups"] == ["admins"]


def test_socket():
    module = load("portal-web.py")
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "apps.json").write_text('{"schema_version":1,"apps":[]}')
        (directory / "guests.json").write_text('{"guests":[{"vmid":140,"name":"web-gateway"}]}')
        with patch.object(module, "CATALOG", directory):
            sock = directory / "portal.sock"
            with module.PortalServer(str(sock), module.Handler) as server:
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                try:
                    def call(path, method="GET", headers=None, body=None):
                        conn = module.UnixConnection(sock)
                        conn.request(method, path, body=body, headers=headers or {})
                        res = conn.getresponse()
                        code = res.status
                        try:
                            payload = json.loads(res.read())
                        finally:
                            conn.close()
                        return code, payload
                    assert call("/api/apps")[0] == 403
                    assert call("/api/apps", headers={"X-Portal-Verified":"admin"})[0] == 403
                    code, data = call("/api/apps", headers={"X-Portal-Verified":"home"})
                    assert code == 200 and data["apps"] == []
                    assert call("/api/admin/users",headers={"X-Portal-Verified":"home"})[0] == 403
                    assert call("/api/admin/guests",headers={"X-Portal-Verified":"home"})[0] == 403
                    assert call("/api/admin/guests",headers={"X-Portal-Verified":"admin"}) == (
                        200,{"guests":[{"vmid":140,"name":"web-gateway"}]}
                    )
                    assert call("/api/admin/access",headers={"X-Portal-Verified":"admin"})[0] == 200
                    trusted={"X-Portal-Verified":"admin","Content-Type":"application/json",
                             "X-Requested-With":"web-gateway","Origin":module.ORIGIN}
                    body=b'{"action":"add","username":"test"}'
                    assert call("/api/admin/users","POST",{"X-Portal-Verified":"admin"})[0] == 403
                    for field,value in [
                        ("Origin","https://evil.example"),
                        ("X-Requested-With","bad"),
                        ("Content-Type","text/plain"),
                        ("X-Portal-Verified","home")
                    ]:
                        hdr={**trusted,field:value}
                        assert call("/api/admin/users","POST",hdr)[0] == 403
                    with patch.object(module,"upstream",return_value=(200,b'{"status":"ok"}')) as upstream:
                        assert call("/api/admin/users","POST",trusted,body) == (200,{"status":"ok"})
                        upstream.assert_called_once_with("/management/action","POST",body)
                    assert call("/api/admin/users","POST",trusted,b"[]")[0] == 400
                    assert call("/api/admin/nonexistent",headers=trusted)[0] == 404
                finally:
                    server.shutdown()
                    t.join()


def test_route_migration():
    module = load("migrate-portal-routes.py")
    with TemporaryDirectory() as tmp:
        state = Path(tmp)
        (state / "management").mkdir()
        file = state / "management/routes.yaml"
        old=[
            {"domain":"home.zsergey.ru","service":"homarr","upstream":"http://homarr:7575","enabled":True,"access":"public"},
            {"domain":"check.zsergey.ru","service":"homarr","upstream":"http://homarr:7575","enabled":False,"access":"private"},
            {"domain":"auth.zsergey.ru","service":"authelia","upstream":"http://authelia:9091","enabled":True,"access":"public"},
        ]
        file.write_text(yaml.safe_dump(old))
        with patch.object(module.os.path,"ismount",return_value=True):
            assert module.migrate(state) is True
            new=yaml.safe_load(file.read_text())
            assert all(x["service"] == "portal" for x in new[:2])
            assert new[0]["enabled"] and new[1]["enabled"] is False
            assert new[2] == old[2]
            assert module.migrate(state) is False
            new[0]["upstream"]="http://malicious:1"
            file.write_text(yaml.safe_dump(new))
            try:
                module.migrate(state)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Нельзя менять неожиданное назначение")
        with patch.object(module.os.path,"ismount",return_value=False):
            try:
                module.migrate(state)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Отсутствие mp0 должно блокировать изменения")


if __name__ == "__main__":
    test_policy()
    test_socket()
    test_route_migration()
    print("140: вход защищён, администрация локальна, без CSRF и перезаписи маршрутов")
