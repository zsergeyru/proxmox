#!/usr/bin/env python3
"""Сквозная проверка диагностики infra-web через реальный локальный Unix-сокет."""

from __future__ import annotations

import importlib.util
import socket
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
WEB = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web"
sys.path.insert(0, str(WEB))

from infra_manager import admin_socket
from infra_manager.operation_store import OperationStore


def main() -> None:
    spec = importlib.util.spec_from_file_location("infra_web_diagnostics_integration", WEB / "admin.py")
    assert spec and spec.loader
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        store = OperationStore(base / "operations", require_mount=False)
        record, _ = store.reserve("status", 109, "test-git-revision")
        store.start(record["id"])
        journal = SimpleNamespace(returncode=0, stdout=(
            "[ИНФО] Выполняется проверка\n[ОК] Проверка завершена\n"
            "[ИНФО] password=not-for-browser\n"
        ))
        socket_path = base / "operations.sock"
        with (
            patch.object(admin_socket, "OperationStore", return_value=store),
            patch.object(admin_socket, "run", return_value=journal),
            patch.object(admin, "SOCKET", str(socket_path)),
        ):
            server = admin_socket.AdminSocketServer(socket_path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = admin._operation_detail(record["id"])
                assert result["accepted"] is True
                assert result["operation"]["phase"] == "running"
                assert result["events"][0]["phase"] == "queued"
                assert result["events"][-1]["phase"] == "running"
                assert "not-for-browser" not in str(result)
                assert "[ОК] Проверка завершена" in result["logs"]
                page = admin._operation_page(record["id"]).decode("utf-8")
                assert record["id"] in page and "Диагностика задания" in page
                assert "<script src=\"/diagnostics.js\" defer></script>" in page
                try:
                    admin._operation_detail("../../../etc/passwd")
                    raise AssertionError("Недопустимый идентификатор прошёл проверку")
                except ValueError:
                    pass
                # Старый закрытый посредник не должен получать произвольный
                # путь к файлу или shell из API диагностики.
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(socket_path))
                    connection.sendall((
                        '{"kind":"operation-detail","operation_id":"'
                        + record["id"] + '","request_id":"'
                        + "a" * 32 + '","vmid":0,"path":"/etc/shadow"}\n'
                    ).encode())
                    answer = connection.makefile("rb").readline()
                    assert b'"accepted": false' in answer
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("[ОК] Сквозной Web -> Unix socket -> закрытое состояние и безопасный журнал")


if __name__ == "__main__":
    main()
