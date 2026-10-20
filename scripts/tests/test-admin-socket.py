#!/usr/bin/env python3
"""Проверить закрытый посредник действий без доступа к PVE и Docker."""

from __future__ import annotations

import json
import socket
import stat
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import admin_socket
from infra_manager.common import InfraManagerError
from infra_manager.operation_store import OperationStore


def rejected(action):
    try:
        action()
    except InfraManagerError:
        return True
    return False


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rules = root / "infrastructure/security/access.yaml"
        rules.parent.mkdir(parents=True)
        rules.write_text(yaml.safe_dump({
            "schema_version": 1,
            "rules": [{"subject": "guest:910", "service": "pve-api",
                       "resource": "guests", "access": ["read", "configure"]}],
        }), encoding="utf-8")
        guest = SimpleNamespace(vmid=109, role="managed-guest")
        store = OperationStore(root / "operations", require_mount=False)
        with (
            patch.object(admin_socket, "PATHS", SimpleNamespace(repo_root=root)),
            patch.object(admin_socket, "operation_guests", return_value=[guest]),
            patch.object(admin_socket, "find_guest_by_role", return_value=SimpleNamespace(vmid=910)),
        ):
            admin_socket.authorize_action("status", 109)
            admin_socket.authorize_action("sync", 109)
            assert rejected(lambda: admin_socket.authorize_action("deploy", 110))
            assert rejected(lambda: admin_socket.authorize_action("shell", 109))
            rules.write_text(yaml.safe_dump({"schema_version": 1, "rules": []}))
            assert rejected(lambda: admin_socket.authorize_action("deploy", 109))
        with (
            patch.object(admin_socket, "authorize_action"),
            patch.object(admin_socket, "OperationStore", return_value=store),
            patch.object(admin_socket, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            operation_id = uuid.uuid4().hex
            accepted = admin_socket.dispatch({"kind": "status", "vmid": 109, "request_id": operation_id})
            assert accepted["accepted"] and accepted["id"] == operation_id
            cmd = run.call_args.args[0]
            assert cmd[0] == "systemd-run" and "/usr/bin/python3" in cmd
            assert "--request-id" in cmd and operation_id in cmd
            assert rejected(lambda: admin_socket.dispatch({"kind": "shell", "vmid": 109, "request_id": operation_id}))
            assert rejected(lambda: admin_socket.dispatch({"kind": "status", "vmid": 109, "request_id": "../passwd"}))
            assert rejected(lambda: admin_socket.dispatch({"kind": "status", "vmid": 109, "request_id": operation_id, "cmd": "sh"}))
        # Физический SSD: только два типизированных действия, без shell.
        with (
            patch.object(admin_socket, "authorize_action") as authorize,
            patch.object(admin_socket, "find_guest_by_role",
                         return_value=SimpleNamespace(vmid=920)),
            patch("infra_manager.operator._pve_node", return_value="pve"),
            patch("infra_manager.pve.PveClient"),
            patch("infra_manager.pve_backup_ssd.storage_status",
                  return_value={"state": "needs-confirmation"}) as inspect,
            patch("infra_manager.pve_backup_ssd.approve_backup_ssd") as approve,
        ):
            request = {"kind": "storage-plan", "vmid": 0, "request_id": uuid.uuid4().hex}
            record = admin_socket.dispatch(request)
            assert record["accepted"] and record["storage"]["state"] == "needs-confirmation"
            inspect.assert_called_once()
            authorize.assert_called_with("status", 920)
            request.update({"kind": "storage-confirm",
                            "plan_id": "a" * 64, "serial": "19386J473508"})
            record = admin_socket.dispatch(request)
            assert record["accepted"]
            approve.assert_called_once()
            authorize.assert_called_with("repair", 920)
            assert rejected(lambda: admin_socket.dispatch(dict(request, vmid=901)))
            assert rejected(lambda: admin_socket.dispatch(dict(request, command="mkfs")))
            assert rejected(lambda: admin_socket.dispatch(dict(request, request_id="../etc")))
            assert rejected(lambda: admin_socket.dispatch(dict(request, kind="storage-shell")))
        path = root / "operations.sock"
        server = admin_socket.AdminSocketServer(path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert stat.S_IMODE(path.stat().st_mode) == 0o660
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(path))
                connection.sendall(b'{"kind": "shell"}\n')
                result = connection.recv(2048)
            assert json.loads(result)["accepted"] is False
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert not path.exists()
    print("[ОК] Посредник ограничен списком действий, VMID, access.yaml и UUID")


if __name__ == "__main__":
    main()
