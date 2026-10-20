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
        # Закрытая история: только ограниченные поля, никаких токенов и журналов.
        history_id = uuid.uuid4().hex
        record = {
            "id": history_id, "kind": "deploy", "target": 910,
            "phase": "completed", "created_at": "2026-10-20T10:00:00Z",
            "token": "never-leak", "log": "logs/secret.jsonl",
        }
        with patch.object(
            admin_socket, "OperationStore",
            return_value=SimpleNamespace(list_recent=lambda limit: [record] if limit == 40 else []),
        ):
            request = {"kind": "history", "vmid": 0, "request_id": uuid.uuid4().hex}
            result = admin_socket.dispatch(request)
            assert result["accepted"] is True
            assert result["operations"] == [{
                "id": history_id, "kind": "deploy", "target": 910,
                "phase": "completed", "created_at": "2026-10-20T10:00:00Z",
            }]
            assert "never-leak" not in json.dumps(result)
            assert "secret.jsonl" not in json.dumps(result)
            assert rejected(lambda: admin_socket.dispatch(dict(request, vmid=109)))
            assert rejected(lambda: admin_socket.dispatch(dict(request, path="/etc/passwd")))
            assert rejected(lambda: admin_socket.dispatch(dict(request, request_id="../etc")))
        # Диагностика ограничена собственным UUID, типизированными полями
        # и безопасными сообщениями systemd/активации. Секреты не публиковать.
        diagnostics_store = OperationStore(root / "diagnostics", require_mount=False)
        task, _ = diagnostics_store.reserve("deploy", 910, "abc", activation_required=True)
        diagnostics_store.start(task["id"])
        diagnostics_store.transition(task["id"], "activating")
        diagnostics_store.transition(task["id"], "verifying")
        diagnostics_store.finish(task["id"], exit_code=9)
        activation_log = root / "runtime-activation.log"
        activation_log.write_text(
            "[ИНФО] Операция активации: " + task["id"] + "\n"
            "[ОК] Запущена новая версия\\n"
            "[ОШИБКА] Проверка готовности не пройдена\\n"
            "[ИНФО] Операция активации: " + uuid.uuid4().hex + "\n"
            "[ОК] Чужая операция\\n",
            encoding="utf-8",
        )
        mock_journal = SimpleNamespace(returncode=0, stdout=(
            "[ИНФО] Шаг: получение Git\\n"
            "[ОШИБКА] Permission denied\\n"
            "[ИНФО] password=hunter2\\n"
            "[ИНФО] token=abcdef\\n"
            "SECRET_TEXT_WITHOUT_ALLOWED_PREFIX\\n"
        ))
        with (
            patch.object(admin_socket, "OperationStore", return_value=diagnostics_store),
            patch.object(admin_socket, "ACTIVATION_LOG", activation_log),
            patch.object(admin_socket, "run", return_value=mock_journal) as journal,
        ):
            query = {
                "kind": "operation-detail", "vmid": 0,
                "request_id": uuid.uuid4().hex, "operation_id": task["id"],
            }
            result = admin_socket.dispatch(query)
            assert result["accepted"] is True and result["operation"]["id"] == task["id"]
            assert result["operation"]["exit_code"] == 9
            assert result["operation"]["phase"] == "failed"
            assert [x["phase"] for x in result["events"]] == [
                "queued", "running", "activating", "verifying", "failed",
            ]
            assert "Permission denied" in result["diagnostic"] or "Проверка готовности" in result["diagnostic"]
            visible = json.dumps(result, ensure_ascii=False)
            assert "hunter2" not in visible and "abcdef" not in visible
            assert "SECRET_TEXT" not in visible and "Чужая операция" not in visible
            assert "Запущена новая версия" in visible
            assert journal.call_args.args[0][:3] == ["timeout", "3s", "journalctl"]
            assert journal.call_args.args[0][-1] == f"infra-operation-{task['id']}.service"
            assert rejected(lambda: admin_socket.dispatch(dict(query, operation_id="../passwd")))
            assert rejected(lambda: admin_socket.dispatch(dict(query, path="/etc/passwd")))
            assert rejected(lambda: admin_socket.dispatch(dict(query, vmid=910)))
            assert rejected(lambda: admin_socket.dispatch(dict(query, request_id="not-a-uuid")))
            assert rejected(lambda: admin_socket.dispatch(dict(query, operation_id=uuid.uuid4().hex)))
        assert admin_socket._filter_log(["[ОК] Безопасная строка", "[ИНФО] secret=bad"]) == [
            "[ОК] Безопасная строка",
        ]
        secret_file = root / "sensitive"
        secret_file.write_text("[ОК] Секреты не должны читаться", encoding="utf-8")
        alias = root / "alias"
        alias.symlink_to(secret_file)
        try:
            admin_socket._read_log_tail(alias)
            raise AssertionError("Символьная ссылка была прочитана")
        except OSError:
            pass
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
