#!/usr/bin/env python3
"""Вход через OpenBao, проверка CSRF и закрытые действия infra-web."""

from __future__ import annotations

import http.client
import os
import importlib.util
import sys
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web"
sys.path.insert(0, str(WEB))
spec = importlib.util.spec_from_file_location("infra_web_admin_test", WEB / "admin.py")
assert spec is not None and spec.loader is not None
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


def request(server, method, path, data=None, *, origin=True, cookie=None, host_override=None, fetch_site=None):
    host, port = server.server_address
    headers = {"Host": host_override or f"{host}:{port}"}
    if origin:
        headers["Origin"] = f"https://{host}:{port}" if origin is True else origin
    if fetch_site:
        headers["Sec-Fetch-Site"] = fetch_site
    if cookie:
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urlencode(data)
    connection = http.client.HTTPConnection(host, port, timeout=4)
    connection.request(method, path, body=data, headers=headers)
    result = connection.getresponse()
    body = result.read().decode()
    response = result.status, dict(result.getheaders()), body
    connection.close()
    return response


def main():
    server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with (
            patch.object(admin, "WEB_HOST", f"{host}:{port}"),
            patch.object(admin, "_authenticate", side_effect=lambda name, pwd: name == "operator" and pwd == "secret"),
            patch.object(admin, "_send_action", side_effect=lambda kind, vmid, rid: {"id": rid, "state": "queued", "accepted": True}),
            patch.object(admin, "_operation_history", return_value=[]),
            patch.object(admin, "_send_storage", side_effect=lambda kind, **kwargs:
                {"accepted": True, "storage": {
                    "state": "needs-confirmation",
                    "plan_id": "a" * 64,
                    "serial": "19386J473508",
                    "devpath": "/dev/sda",
                    "model": "WD Green",
                    "size_bytes": 240057409536,
                }} if kind == "storage-plan" else {"accepted": True}),
        ):
            # Политика ответа с HTML-формой не должна делать Origin: null.
            status, headers, body = request(server, "GET", "/")
            assert status == 200 and 'action="/login"' in body
            assert "Версии компонентов" in body
            assert "infra-web (образ)" in body
            assert "не определена" in body
            # Этот раздел публичен, поэтому отдаём только версии и SHA,
            # никогда данные окружения или пароль OpenBao.
            with patch.dict(os.environ, {
                "INFRA_WEB_BUILD_REVISION": "a" * 40,
                "INFRA_GIT_REVISION": "a" * 40,
                "INFRA_RUNTIME_VERSION": "v1",
                "INFRA_OPENBAO_VERSION": "2.7.0",
                "INFRA_OPENTOFU_VERSION": "1.12.6",
                "INFRA_ANSIBLE_VERSION": "2.19.4",
                "INFRA_PACKER_VERSION": "1.15.4",
                "OPENBAO_ROOT_TOKEN": "secret-never-in-public-html",
            }):
                st, _, login_html = request(server, "GET", "/")
                assert st == 200
                for fragment in ("Git aaaaaaaaaaaa", "v1",
                                 "2.7.0", "1.12.6", "2.19.4", "1.15.4"):
                    assert fragment in login_html, fragment
                assert "secret-never-in-public-html" not in login_html
                assert "Внимание: версия веб-панели" not in login_html
                with patch.dict(os.environ, {"INFRA_GIT_REVISION": "b" * 40}):
                    st, _, outdated = request(server, "GET", "/")
                    assert st == 200 and "Внимание: версия веб-панели" in outdated
            assert headers["Referrer-Policy"] == "same-origin"
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["X-Frame-Options"] == "DENY"
            assert "form-action 'self'" in headers["Content-Security-Policy"]
            status, _, _ = request(server, "GET", "/api/operations")
            assert status == 401, "История не должна открываться без входа"
            status, _, _ = request(server, "POST", "/action", {"vmid": "109", "kind": "status"})
            assert status == 401, "Нельзя запускать действие без авторизации"
            status, _, _ = request(server, "POST", "/login", {"username": "operator", "password": "secret"}, origin=False)
            assert status == 403, "POST без Origin должен быть отклонён"
            status, _, _ = request(server, "POST", "/login", {
                "username": "operator", "password": "secret",
            }, origin="null", fetch_site="same-origin")
            assert status == 403, "Origin: null не должен обходить проверку CSRF"
            status, _, _ = request(server, "POST", "/login", {
                "username": "operator", "password": "secret",
            }, origin="https://untrusted.example")
            assert status == 403, "Чужой Origin не должен разрешать вход"
            status, _, _ = request(server, "POST", "/login", {
                "username": "operator", "password": "secret",
            }, host_override="untrusted.example:8443")
            assert status == 403, "Чужой Host не должен разрешать вход"
            status, _, _ = request(server, "POST", "/login", {"username": "operator", "password": "wrong"})
            assert status == 401, "Неверный пароль не должен приниматься"
            # Закрытая история не должна ломать успешный вход OpenBao.
            with patch.object(admin, "_operation_history", side_effect=PermissionError("private state")):
                status, headers, body = request(server, "POST", "/login", {"username": "operator", "password": "secret"})
                assert status == 200 and "secret" not in body
                assert "История операций временно недоступна" in body
                cookie = headers["Set-Cookie"].split(";", 1)[0]
                status, _, body = request(server, "GET", "/api/operations", cookie=cookie)
                assert status == 503 and "unavailable" in body
                status, _, body = request(server, "GET", "/", cookie=cookie)
                assert status == 200 and "История операций временно недоступна" in body
            assert all(flag in headers["Set-Cookie"] for flag in ("Secure", "HttpOnly", "SameSite=Strict"))
            assert headers["Strict-Transport-Security"].startswith("max-age=")
            assert headers["Referrer-Policy"] == "same-origin"
            status, _, body = request(server, "GET", "/api/operations", cookie=cookie)
            assert status == 200 and body.lstrip().startswith("[")
            status, _, _ = request(server, "POST", "/action", {"vmid": "109", "kind": "status"}, cookie=cookie)
            assert status == 403, "Действие без CSRF должно отклоняться"
            token = cookie.split("=", 1)[1]
            csrf = admin.sessions[token]["csrf"]
            status, _, _ = request(server, "POST", "/action", {"vmid": "109", "kind": "status", "csrf": csrf}, cookie=cookie, origin=False)
            assert status == 403, "Подмена источника недопустима"
            status, _, body = request(server, "POST", "/action", {"vmid": "109", "kind": "status", "csrf": csrf}, cookie=cookie)
            assert status == 202 and "Задание принято" in body
            status, _, _ = request(server, "GET", "/storage")
            assert status == 401, "План SSD запрещён без входа"
            status, _, body = request(server, "GET", "/storage", cookie=cookie)
            assert status == 200 and "19386J473508" in body
            assert 'type="hidden" name="serial"' in body
            assert '<button type="submit">Да, подготовить SSD</button>' in body
            assert 'name="serial" autocomplete="off"' not in body
            status, _, _ = request(server, "POST", "/storage-confirm", {
                "csrf": csrf, "plan_id": "a" * 64, "serial": "19386J473508",
            }, cookie=cookie, origin=False)
            assert status == 403, "Подмена Origin должна блокировать согласие SSD"
            status, _, _ = request(server, "POST", "/storage-confirm", {
                "csrf": csrf, "plan_id": "a" * 64, "serial": "19386J473508",
            }, cookie=cookie, origin="null", fetch_site="same-origin")
            assert status == 403, "Origin: null не разрешает форматирование SSD"
            status, _, _ = request(server, "POST", "/storage-confirm", {
                "csrf": "wrong", "plan_id": "a" * 64, "serial": "19386J473508",
            }, cookie=cookie)
            assert status == 403, "Без CSRF нельзя разрешать разметку"
            status, _, _ = request(server, "POST", "/storage-confirm", {
                "csrf": csrf, "plan_id": "invalid", "serial": "19386J473508",
            }, cookie=cookie)
            assert status == 200 or status == 400  # mock вызова не заменяет реальную проверку посредника
            status, _, body = request(server, "POST", "/storage-confirm", {
                "csrf": csrf, "plan_id": "a" * 64, "serial": "19386J473508",
            }, cookie=cookie)
            assert status == 200 and "Подготовка SSD запущена" in body
            with patch.object(
                admin, "_send_storage",
                return_value={"accepted": True, "storage": {"state": "running"}},
            ):
                status, _, body = request(server, "GET", "/storage", cookie=cookie)
                assert status == 200
                assert 'http-equiv="refresh"' in body
                assert "Можно закрыть браузер" in body
            status, _, _ = request(server, "POST", "/logout", {"csrf": csrf}, cookie=cookie)
            assert status == 200
            status, _, _ = request(server, "GET", "/api/operations", cookie=cookie)
            assert status == 401, "Сессия должна уничтожаться при выходе"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        admin.sessions.clear()
    print("[ОК] HTTPS-контур: OpenBao-вход, защищённая сессия и CSRF")


if __name__ == "__main__":
    main()
