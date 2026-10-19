#!/usr/bin/env python3
"""Проверить безопасный предварительный просмотр операций infra-web."""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import uuid
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web/app.py"
spec = importlib.util.spec_from_file_location("infra_web_preview", APP)
assert spec is not None and spec.loader is not None
web = importlib.util.module_from_spec(spec)
sys.modules["infra_web_preview"] = web
spec.loader.exec_module(web)


def request(server, method: str, path: str) -> tuple[int, str, dict]:
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout=4)
    conn.request(method, path)
    response = conn.getresponse()
    body = response.read().decode("utf-8")
    headers = dict(response.getheaders())
    conn.close()
    return response.status, body, headers


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        history = root / "history"
        history.mkdir()
        ident = uuid.uuid4().hex
        data = {
            "id": ident, "kind": "<script>alert(1)</script>",
            "target": 109, "phase": "running",
            "created_at": "2026-10-19T12:00:00Z",
        }
        (history / f"{ident}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        (history / f"{uuid.uuid4().hex}.json").symlink_to("/etc/passwd")
        (history / f"{uuid.uuid4().hex}.json").write_text(
            "{" + "x" * 70000, encoding="utf-8"
        )
        with patch.object(web, "STATE", root):
            records = web._history()
            assert len(records) == 1 and records[0]["id"] == ident
            page = web._page(records).decode()
            assert "<script>" not in page and "&lt;script&gt;" in page
            server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                code, body, headers = request(server, "GET", "/")
                assert code == HTTPStatus.OK and "infra-manager" in body
                assert headers.get("Cache-Control") == "no-store"
                code, body, _ = request(server, "GET", "/api/operations")
                assert code == HTTPStatus.OK and json.loads(body)[0]["id"] == ident
                code, body, _ = request(server, "GET", "/health")
                assert code == HTTPStatus.OK and body == "ok\n"
                code, body, _ = request(server, "POST", "/")
                assert code == HTTPStatus.METHOD_NOT_ALLOWED
                code, body, _ = request(server, "GET", "/etc/passwd")
                assert code == HTTPStatus.NOT_FOUND
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("[ОК] infra-web: чтение истории, защита HTML, только GET, закрытые пути")


if __name__ == "__main__":
    main()
