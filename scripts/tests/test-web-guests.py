#!/usr/bin/env python3
"""Каталог гостей и страницы управления: реальные исходники Git, без PVE/секретов."""

from __future__ import annotations

import importlib.util
import http.client
import json
import socket
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
WEB = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web"
sys.path.insert(0, str(WEB))

from infra_manager import admin_socket
from infra_manager.common import InfraManagerError
from infra_manager.guest_catalog import deployable_guests, load_manifest
from infra_manager.operation_store import OperationStore

spec = importlib.util.spec_from_file_location("infra_web_guest_catalog_test", WEB / "admin.py")
assert spec and spec.loader
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


def request(server, route, *, cookie=None):
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=4)
    headers = {"Host": f"{host}:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    connection.request("GET", route, headers=headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def main():
    guests = deployable_guests(ROOT)
    assert guests and any(guest.vmid == 910 for guest in guests)
    selected = next(guest for guest in guests if guest.vmid != 910)
    pve_data = []
    profiles = load_manifest(ROOT / "infrastructure/guests/defaults.yaml")["profiles"]
    for guest in guests:
        guest_type = profiles[guest.source["profile"]]["type"]
        typ = "qemu" if guest_type == "vm" else "lxc"
        # Достаточно реально объявленных гостей, без угадывания состояния других.
        if guest.vmid == selected.vmid:
            pve_data.append({
                "vmid": guest.vmid, "name": guest.name, "type": typ,
                "status": "running",
            })
    mock_client = SimpleNamespace(data=lambda *args, **kwargs: pve_data)
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        store = OperationStore(base / "operations", require_mount=False)
        target_record, _ = store.reserve("status", selected.vmid, "project-sha")
        store.start(target_record["id"])
        store.finish(target_record["id"], exit_code=0)
        another, _ = store.reserve("status", 910, "project-sha")
        store.start(another["id"])
        store.finish(another["id"], exit_code=0)
        socket_path = base / "operations.sock"
        with (
            patch.object(admin_socket, "PATHS", SimpleNamespace(repo_root=ROOT)),
            patch.object(admin_socket, "PveClient", return_value=mock_client) as pve,
            patch.object(admin_socket, "OperationStore", return_value=store),
            patch.object(admin_socket, "authorize_action") as authorize,
            patch.object(admin, "SOCKET", str(socket_path)),
        ):
            server = admin_socket.AdminSocketServer(socket_path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                catalog = admin._guest_catalog("guests")
                assert catalog["pve_available"] is True
                assert len(catalog["guests"]) == len(guests)
                row = next(guest for guest in catalog["guests"] if guest["vmid"] == selected.vmid)
                assert row["state"] == "running"
                assert row["name"] == selected.name
                assert row["actions"] and "status" in row["actions"]
                assert pve.call_count == 1, "Нельзя вызывать PVE по одному разу на каждый VMID"
                owner = next(guest for guest in catalog["guests"] if guest["vmid"] == 910)
                assert "deploy" not in owner["actions"], "910 обновляется только через PVE CLI"
                assert all(not any(key in row for key in ("token", "secret", "private_key"))
                           for row in catalog["guests"])
                detail = admin._guest_catalog("guest-detail", selected.vmid)
                assert detail["guest"]["vmid"] == selected.vmid
                assert [x["id"] for x in detail["operations"]] == [target_record["id"]]
                assert another["id"] not in json.dumps(detail)
                assert 'href="/operations?id=' + target_record["id"] in (
                    admin._guest_page(selected.vmid, "csrf").decode()
                )
                assert "Да, выполнить" in admin._guest_confirmation(
                    selected.vmid, "repair", "csrf").decode()

                # Недоступность API не превращается в ложное "гость выключен".
                with patch.object(admin_socket, "PveClient", side_effect=InfraManagerError("PVE offline")):
                    degraded = admin._guest_catalog("guests")
                    assert degraded["pve_available"] is False
                    assert all(guest["state"] == "unavailable" for guest in degraded["guests"])
                with patch.object(admin_socket, "PveClient", return_value=SimpleNamespace(
                    data=lambda *args, **kwargs: pve_data * 2
                )):
                    degraded = admin._guest_catalog("guests")
                    assert degraded["pve_available"] is False, "PVE с дубликатами VMID опасен"
                assert authorize.called

                try:
                    admin._guest_catalog("guest-detail", 999999)
                    raise AssertionError("Неизвестный VMID должен отклоняться")
                except ValueError:
                    pass
                # Реальная обработка запроса в сокете: никакого произвольного пути.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.connect(str(socket_path))
                    data = {
                        "kind": "guest-detail", "vmid": selected.vmid,
                        "request_id": uuid.uuid4().hex, "path": "/etc/shadow",
                    }
                    connection.sendall(json.dumps(data).encode() + b"\n")
                    assert json.loads(connection.makefile("rb").readline())["accepted"] is False
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    # HTTP маршруты требуют защищённую сессию и правильную форму запроса.
    http_server = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            patch.object(admin, "WEB_HOST", f"127.0.0.1:{http_server.server_port}"),
            patch.object(admin, "_guest_catalog", side_effect=lambda kind, vmid=0: (
                {"accepted": True, "pve_available": True, "guests": [
                    {"vmid": selected.vmid, "name": "<script>", "role": "test",
                     "type": "lxc", "address": "192.168.1.22", "state": "running", "actions": ["status", "repair"]}
                ]} if kind == "guests" else
                {"accepted": True, "pve_available": True, "history_available": True,
                 "guest": {"vmid": vmid, "name": "<script>", "role": "test", "type": "lxc",
                           "address": "192.168.1.22", "state": "running", "actions": ["status", "repair"]},
                 "operations": [{"id": "a" * 32, "kind": "status",
                                  "phase": "completed", "created_at": "2026-10-20T12:00:00Z"}]}
            )),
        ):
            for route in ("/guests", f"/guests?vmid={selected.vmid}",
                          "/api/guests", f"/api/guest?vmid={selected.vmid}",
                          f"/guests/confirm?vmid={selected.vmid}&kind=repair"):
                assert request(http_server, route)[0] == 401, "Без входа каталог закрыт"
            session_id, session = admin._new_session()
            cookie = admin.COOKIE + "=" + session_id
            status, headers, body = request(http_server, "/guests", cookie=cookie)
            assert status == 200 and "&lt;script&gt;" in body and "<script>" not in body
            assert f"/guests?vmid={selected.vmid}" in body
            status, _, body = request(http_server, f"/guests?vmid={selected.vmid}", cookie=cookie)
            assert status == 200 and "Последние задания" in body
            assert "a" * 32 in body
            assert "&lt;script&gt;" in body
            assert 'name="kind" value="status"' in body
            status, _, body = request(
                http_server, f"/guests/confirm?vmid={selected.vmid}&kind=repair",
                cookie=cookie,
            )
            assert status == 200 and "Подтвердите действие" in body
            assert 'name="csrf"' in body
            for route in ("/guests?vmid=../etc", "/guests?vmid=910&path=/etc",
                          "/guests/confirm?vmid=910&kind=shell",
                          "/api/guest?vmid=910&kind=deploy"):
                assert request(http_server, route, cookie=cookie)[0] == 400
            status, _, body = request(http_server, "/api/guests", cookie=cookie)
            assert status == 200 and json.loads(body)["guests"][0]["vmid"] == selected.vmid
            status, _, body = request(http_server, f"/api/guest?vmid={selected.vmid}", cookie=cookie)
            assert status == 200 and json.loads(body)["guest"]["vmid"] == selected.vmid
            admin.sessions.pop(session_id)
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=5)
    print("[ОК] Гости: каталог из Git, PVE состояние, права, история, HTML и API")


if __name__ == "__main__":
    main()
