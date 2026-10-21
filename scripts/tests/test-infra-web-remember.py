#!/usr/bin/env python3
"""Длительный вход: opt-in, серверное хранение, отзыв, TTL и защита cookie."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web"
sys.path.insert(0, str(WEB))
spec = importlib.util.spec_from_file_location("infra_web_remember", WEB / "admin.py")
assert spec and spec.loader
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


def call(server, path, *, cookie=None, fields=None, origin=True):
    host, port = server.server_address
    headers = {"Host": f"{host}:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    if fields is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if origin:
            headers["Origin"] = f"https://{host}:{port}"
        body = urlencode(fields)
    else:
        body = None
    client = http.client.HTTPConnection(host, port, timeout=6)
    client.request("POST" if fields is not None else "GET", path, body=body, headers=headers)
    result = client.getresponse()
    data = result.read().decode()
    status = result.status
    response = (status, dict(result.getheaders()), data)
    client.close()
    return response


def main():
    with tempfile.TemporaryDirectory() as directory:
        storage = Path(directory) / "sessions"
        storage.mkdir(mode=0o700)
        with patch.object(admin, "SESSION_DIR", storage):
            # Отсутствующее хранилище не должно молча выдавать одночасовую сессию.
            storage.chmod(0o755)
            try:
                admin._new_session(remember=True)
                raise AssertionError("Нельзя сохранять cookie при открытых правах каталога")
            except ValueError:
                pass
            storage.chmod(0o700)
            token, session = admin._new_session(remember=True)
            assert len(token) >= 32
            assert session["remember"] is True
            assert 29 * 86400 <= session["expires"] - time.time() <= admin.REMEMBER_TTL
            assert token not in admin.sessions
            files = list(storage.glob("*.json"))
            assert len(files) == 1
            assert token not in files[0].name
            assert files[0].stat().st_mode & 0o777 == 0o600
            assert token not in files[0].read_text()
            saved = admin._session(admin.COOKIE + "=" + token)
            assert saved is not None and saved[1]["csrf"] == session["csrf"]
            admin.sessions.clear()  # Имитация перезапуска Docker-контейнера.
            assert admin._session(admin.COOKIE + "=" + token) is not None

            # Удалённый долгий сеанс перестаёт работать после выхода.
            admin._revoke_session(token, session)
            assert not list(storage.glob("*.json"))
            assert admin._session(admin.COOKIE + "=" + token) is None

            # Прошедший срок не продлевается при просмотре страниц.
            token, session = admin._new_session(remember=True)
            path = next(storage.glob("*.json"))
            expired = json.loads(path.read_text())
            expired["expires"] = int(time.time()) - 1
            path.write_text(json.dumps(expired))
            path.chmod(0o600)
            assert admin._session(admin.COOKIE + "=" + token) is None

            # Символические ссылки вместо сессий не читаются.
            token, _ = admin._new_session(remember=True)
            path = next(storage.glob("*.json"))
            path.unlink()
            path.symlink_to("/etc/passwd")
            assert admin._session(admin.COOKIE + "=" + token) is None
            path.unlink()

            with (
                patch.object(admin, "WEB_HOST", "127.0.0.1:8443"),
                patch.object(admin, "_authenticate", return_value=True),
                patch.object(admin, "_operation_history", return_value=[]),
            ):
                server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with patch.object(admin, "WEB_HOST", f"127.0.0.1:{server.server_port}"):
                        login = {"username": "operator", "password": "example"}
                        status, headers, body = call(server, "/login", fields={**login, "remember": "30d"})
                        assert status == 303 and body == ""
                        cookie = headers["Set-Cookie"]
                        assert f"Max-Age={admin.REMEMBER_TTL}" in cookie
                        assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=Strict"))
                        token = cookie.split("=", 1)[1].split(";", 1)[0]
                        session_cookie = admin.COOKIE + "=" + token
                        admin.sessions.clear()
                        status, _, page = call(server, "/", cookie=session_cookie)
                        assert status == 200 and 'class="nav"' in page
                        csrf = admin._session(session_cookie)[1]["csrf"]
                        status, headers, _ = call(server, "/logout", cookie=session_cookie, fields={"csrf": csrf})
                        assert status == 303
                        assert "Max-Age=0" in headers["Set-Cookie"]
                        assert admin._session(session_cookie) is None
                        status, _, _ = call(server, "/api/operations", cookie=session_cookie)
                        assert status == 401
                        status, headers, _ = call(server, "/login", fields=login)
                        assert status == 303 and "Max-Age=" not in headers["Set-Cookie"]
                        short_cookie = headers["Set-Cookie"].split(";", 1)[0]
                        assert admin._session(short_cookie) is not None
                        admin.sessions.clear()
                        assert admin._session(short_cookie) is None
                        assert call(server, "/login", fields={**login, "remember": "forever"})[0] == 400
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                    admin.sessions.clear()
    print("[ОК] Запомнить вход: 30 дней, защищённый cookie, перезапуск, отзыв, TTL, symlink")


if __name__ == "__main__":
    main()
