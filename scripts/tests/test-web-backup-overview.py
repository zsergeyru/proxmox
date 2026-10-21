#!/usr/bin/env python3
"""Третий этап infra-web: реальные декларации, приватные результаты и безопасный systemd."""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
WEB = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web"
sys.path.insert(0, str(WEB))

from infra_manager import admin_socket, backup_controller, backup_overview
from infra_manager.common import InfraManagerError
from infra_manager.operation_store import OperationStore, _atomic_json

spec = importlib.util.spec_from_file_location("infra_web_backup_phase3", WEB / "admin.py")
assert spec and spec.loader
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


def send(server, route, *, cookie=None, post=None, origin=None):
    host, port = server.server_address
    header = {"Host": f"{host}:{port}"}
    if cookie:
        header["Cookie"] = cookie
    if post is not None:
        header["Content-Type"] = "application/x-www-form-urlencoded"
        header["Origin"] = origin if origin is not None else f"https://{host}:{port}"
        data = urlencode(post)
    else:
        data = None
    conn = http.client.HTTPConnection(host, port, timeout=4)
    conn.request("POST" if post is not None else "GET", route, body=data, headers=header)
    resp = conn.getresponse()
    result = resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
    conn.close()
    return result


def saves(store, core=None, full=None, vm100=None):
    for name, record in (("core", core), ("full", full), ("vm100", vm100)):
        path = store.settings / backup_overview.RECORD_NAMES[name]
        if record is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_json(path, record)


def main():
    # Видимый в интерфейсе результат предыдущего systemd-задания должен
    # классифицироваться, но не передавать сырые/секретные строки journalctl.
    with patch.object(
        backup_overview, "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="[ОЖИДАНИЕ] Резервирование: Нет доступного места: "
                   "fast-ssd: PVE не подтвердил действие резервирования: check\n",
        ),
    ):
        assert "PVE отверг проверку" in backup_overview._backup_failure_hint()
    with patch.object(
        backup_overview, "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="[ОЖИДАНИЕ] Резервирование: "
                   "PVE: не подготовлен AppRole снимка OpenBao\n",
        ),
    ):
        assert "AppRole" in backup_overview._backup_failure_hint()
    with patch.object(
        backup_overview, "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="[ОЖИДАНИЕ] Резервирование: Некоторые назначения "
                   "резервирования недоступны: fast-ssd: "
                   "Проверка или запись restic в fast-ssd завершилась с ошибкой\n",
        ),
    ):
        assert "ошибка restic" in backup_overview._backup_failure_hint()
    for stage, expected in (
        ("создание репозитория", "не создал репозиторий"),
        ("проверка доступа к репозиторию", "не смог открыть репозиторий"),
        ("запись архива", "не смог записать архив"),
        ("проверка целостности архива", "не смог завершить проверку"),
    ):
        with patch.object(
            backup_overview, "run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="[ОЖИДАНИЕ] Резервирование: fast-ssd: "
                       "restic: " + stage + " на fast-ssd завершилась ошибкой\n",
            ),
        ):
            assert expected in backup_overview._backup_failure_hint()
    with patch.object(
        backup_overview, "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="[ОЖИДАНИЕ] Резервирование: password=very-secret\n",
        ),
    ):
        assert "very-secret" not in backup_overview._backup_failure_hint()
    with patch.object(
        backup_overview, "run",
        return_value=SimpleNamespace(
            returncode=0,
            stdout="[ОЖИДАНИЕ] Резервирование: Некоторые назначения "
                   "резервирования недоступны: volume:gogs_data: "
                   "fast-ssd: Том gogs_data: экспорт PVE завершился с ошибкой "
                   "(код 1; причина: LVM не создал временный snapshot)\n",
        ),
    ):
        assert "LVM не создал временный снимок" in backup_overview._backup_failure_hint()
    with tempfile.TemporaryDirectory() as directory:
        store = OperationStore(Path(directory) / "operations", require_mount=False)
        pve = SimpleNamespace(data=lambda *args, **kwargs: [
            {"storage": "backup-fast", "active": 1},
            {"storage": "nas-backup", "active": 0},
        ])
        timer = {"state": "active", "service": "inactive", "last_result": "success",
                 "next": "Tue 2026-10-20 21:30:00", "last": "Tue 2026-10-20 15:30:00"}
        with (
            patch.object(backup_overview, "PATHS", SimpleNamespace(repo_root=ROOT)),
            patch.object(backup_overview, "OperationStore", return_value=store),
            patch.object(backup_overview, "PveClient", create=True, return_value=pve),
            patch("infra_manager.pve.PveClient", return_value=pve) as pve_client,
            patch("infra_manager.operator._pve_node", return_value="pve"),
            patch("infra_manager.pve_backup_ssd._read_state", return_value=None),
            patch("infra_manager.pve_backup_ssd._read_job", return_value=None),
            patch.object(backup_overview, "_timer", return_value=timer),
        ):
            first = backup_overview.snapshot()
            assert first["backup"]["overall"] == "no_confirmed_backup"
            assert first["backup"]["timer"]["state"] == "active"
            assert len(first["storage"]["volumes"]) == 3
            assert len(first["nas"]["volumes"]) == 3
            assert all(x["mount_check"] == "not_checked" for x in first["storage"]["volumes"])
            assert first["destinations"][0]["pve_state"] == "reported_active", (first["destinations"], first["pve_api_available"], pve_client.call_count)
            assert first["destinations"][1]["pve_state"] == "reported_inactive"
            assert not any(x["configured"] for x in first["destinations"])
            assert pve_client.call_count == 1

            stamp = datetime.now(UTC).isoformat()
            core = {
                "schema_version": 1, "created_utc": stamp,
                "destinations": ["fast-ssd"], "includes_vm100": False,
                "verified": "restic-check-read-data-subset-1-percent",
                "restore_tested": False,
            }
            full = {
                "schema_version": 1, "created_utc": stamp,
                "destinations": ["fast-ssd"], "includes_vm100": True,
                "restore_tested": False,
            }
            vm100 = {
                "date": datetime.now(UTC).date().isoformat(),
                "verified": True, "destinations": ["fast-ssd"],
            }
            saves(store, core=core, full=full, vm100=vm100)
            single = backup_overview.snapshot()
            assert single["backup"]["overall"] == "single_copy"
            assert single["backup"]["independent_recorded_copies"] == 1
            assert single["backup"]["records"]["core"]["check"] == "restic_subset_1_percent"
            assert single["backup"]["records"]["vm100"]["state"] == "verified"
            assert single["backup"]["restore_tested"] is False
            assert single["backup"]["live_media_verified"] is False

            saves(store, core={**core, "destinations": ["fast-ssd", "nas-backup"]})
            twice = backup_overview.snapshot()
            assert twice["backup"]["overall"] == "two_recorded_copies"
            assert not twice["backup"]["live_media_verified"]
            saves(store, core={**core, "created_utc": (
                datetime.now(UTC) - timedelta(hours=12)).isoformat()})
            assert backup_overview.snapshot()["backup"]["overall"] == "stale"
            saves(store, core={**core, "destinations": ["unknown-media"]})
            bad = backup_overview.snapshot()
            assert bad["backup"]["records"]["core"]["state"] == "invalid"
            assert bad["backup"]["overall"] == "no_confirmed_backup"
            (store.settings / "backup-last-core.json").unlink()
            (store.settings / "backup-last-core.json").symlink_to("/etc/passwd")
            assert backup_overview.snapshot()["backup"]["records"]["core"]["state"] == "invalid"
            (store.settings / "backup-last-core.json").unlink()
            saves(store, core=core)

            with patch("infra_manager.pve.PveClient", side_effect=InfraManagerError("PVE offline")):
                offline = backup_overview.snapshot()
                assert offline["pve_api_available"] is False
                assert all(x["pve_state"] == "not_checked" for x in offline["destinations"])

            # Типизированный системный посредник: никаких произвольных программ/путей.
            client = SimpleNamespace(vmid=910)
            request = {"kind": "backup-overview", "vmid": 0, "request_id": uuid.uuid4().hex}
            with (patch.object(admin_socket, "find_guest_by_role", return_value=client),
                  patch.object(admin_socket, "authorize_action") as permission,
                  patch.object(backup_overview, "snapshot", return_value=single)):
                answer = admin_socket.dispatch(request)
                assert answer["accepted"] is True
                assert answer["overview"] == single
                permission.assert_called_with("status", 910)
            for extra in ({"path": "/etc/shadow"}, {"argv": ["bash"]}):
                try:
                    admin_socket.dispatch(dict(request, **extra))
                    raise AssertionError("Произвольное поле запроса должно отклоняться")
                except InfraManagerError:
                    pass
            with (patch.object(admin_socket, "find_guest_by_role", return_value=client),
                  patch.object(admin_socket, "authorize_action"),
                  patch.object(backup_controller, "policy", return_value=({}, {"fast-ssd": "A"})),
                  patch.object(admin_socket, "run", side_effect=[
                      SimpleNamespace(returncode=3, stdout=""),
                      SimpleNamespace(returncode=0, stdout="LoadState=loaded\n"),
                      SimpleNamespace(returncode=0, stdout=""),
                  ]) as systemctl):
                accepted = admin_socket.dispatch({
                    "kind": "backup-run", "vmid": 0, "request_id": uuid.uuid4().hex,
                })
                assert accepted["accepted"] is True
                assert systemctl.call_count == 3
                assert systemctl.call_args.args[0] == [
                    "timeout", "5s", "systemctl", "start", "--no-block",
                    "infra-manager-backup.service",
                ]
            with (patch.object(admin_socket, "find_guest_by_role", return_value=client),
                  patch.object(admin_socket, "authorize_action"),
                  patch.object(backup_controller, "policy", return_value=({}, {"fast-ssd": "A"})),
                  patch.object(admin_socket, "run", return_value=SimpleNamespace(returncode=0))):
                repeated = admin_socket.dispatch({
                    "kind": "backup-run", "vmid": 0, "request_id": uuid.uuid4().hex,
                })
                assert repeated["accepted"] is False
                assert repeated["code"] == "already_running"
            with (patch.object(admin_socket, "find_guest_by_role", return_value=client),
                  patch.object(admin_socket, "authorize_action"),
                  patch.object(backup_controller, "policy", side_effect=InfraManagerError("secret-value")),
                  patch.object(admin_socket, "run") as no_command):
                denied = admin_socket.dispatch({
                    "kind": "backup-run", "vmid": 0, "request_id": uuid.uuid4().hex,
                })
                assert denied["code"] == "media_not_ready"
                assert "secret-value" not in json.dumps(denied)
                no_command.assert_not_called()
            with (patch.object(admin_socket, "find_guest_by_role", return_value=client),
                  patch.object(admin_socket, "authorize_action"),
                  patch.object(backup_controller, "policy", return_value=({}, {"fast-ssd": "A"})),
                  patch.object(admin_socket, "run", side_effect=[
                      SimpleNamespace(returncode=3, stdout=""),
                      SimpleNamespace(returncode=0, stdout="LoadState=not-found\n"),
                  ])):
                absent = admin_socket.dispatch({
                    "kind": "backup-run", "vmid": 0, "request_id": uuid.uuid4().hex,
                })
                assert absent["code"] == "service_missing"

            # Динамический HTML экранируется; ни одна страница не доступна без входа.
            injected = json.loads(json.dumps(single))
            injected["storage"]["volumes"][0]["id"] = "<script>alert(1)</script>"
            injected["backup"]["records"]["core"]["destinations"] = ["fast-ssd"]
            injected["backup"]["records"]["volume_gogs_data"]["last_errors"] = {
                "fast-ssd": "LVM не создал временный snapshot"
            }
            injected["backup"]["timer"]["last_result"] = "exit-code"
            injected["backup"]["timer"]["failure_hint"] = (
                "Не хватает обязательного PVE-only компонента резервирования"
            )
            http = admin.ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                def responder(kind):
                    if kind == "backup-overview":
                        return {"accepted": True, "overview": injected}
                    return {"accepted": True, "message": "systemd accepted"}

                with (patch.object(admin, "_backup_request", side_effect=responder),
                      patch.object(admin, "WEB_HOST", f"127.0.0.1:{http.server_port}")):
                    for route in ("/infrastructure", "/infrastructure/confirm",
                                  "/api/infrastructure"):
                        assert send(http, route)[0] == 401
                    sid, session = admin._new_session()
                    cookie = admin.COOKIE + "=" + sid
                    status, headers, body = send(http, "/infrastructure", cookie=cookie)
                    assert status == 200 and "&lt;script&gt;" in body
                    assert "<script>alert(1)</script>" not in body
                    assert "Последний запуск завершился с ошибкой" in body
                    assert "Не хватает обязательного PVE-only компонента" in body
                    assert "Последнее резервирование не выполнено" in body
                    assert 'id="backup-refreshed"' in body
                    assert 'id="backup-service-state"' in body
                    assert 'data-backup-record="core"' in body
                    assert 'data-backup-destination="fast-ssd"' in body
                    assert 'id="backup-running"' in body
                    assert 'id="backup-technical-details"' in body
                    assert 'data-backup-copy="core"' in body
                    assert 'data-backup-copy="vm100"' in body
                    assert 'data-backup-copy="volume_infra_state"' in body
                    assert 'data-backup-copy="volume_gogs_data"' in body
                    assert 'data-backup-copy-error' in body
                    assert "Ошибка последней попытки: SSD: LVM не создал временный snapshot" in body
                    assert "40 ГиБ" in body and "30 ГиБ" in body
                    assert "Автообновление каждые 4 секунды" in body
                    assert 'id="backup-failure"' in body
                    assert body.count('data-backup-refresh') == 1
                    assert 'src="/backup-refresh.js"' in body
                    assert '<meta http-equiv="refresh"' not in body
                    # Успешное резервирование с одной копией на SSD —
                    # основное сообщение об успехе, а не только предупреждение о NAS.
                    injected["backup"]["timer"]["last_result"] = "success"
                    injected["backup"]["timer"].pop("failure_hint")
                    _, _, good_page = send(http, "/infrastructure", cookie=cookie)
                    assert "Копии PVE/910 и Home Assistant созданы" in good_page
                    assert "NAS: пока не настроен" in good_page
                    assert "Не выполняется" not in good_page
                    assert "не является испытанием восстановления" in body
                    assert send(http, "/backup-refresh.js")[0] == 401
                    js_status, _, js = send(http, "/backup-refresh.js", cookie=cookie)
                    assert js_status == 200
                    assert 'fetch("/api/infrastructure"' in js
                    assert 'setInterval(' in js and '4000' in js
                    status, _, data = send(http, "/api/infrastructure", cookie=cookie)
                    assert status == 200
                    assert json.loads(data)["backup"]["overall"] == "single_copy"
                    status, _, page = send(http, "/infrastructure/confirm", cookie=cookie)
                    assert status == 200 and "Да, начать резервирование" in page
                    assert send(http, "/infrastructure?bad=1", cookie=cookie)[0] == 400
                    assert send(http, "/infrastructure/run", cookie=cookie,
                                post={"csrf": "bad"})[0] == 403
                    assert send(http, "/infrastructure/run", cookie=cookie,
                                post={"csrf": session["csrf"]},
                                origin="https://evil.example")[0] == 403
                    status, headers, content = send(
                        http, "/infrastructure/run", cookie=cookie,
                        post={"csrf": session["csrf"]},
                    )
                    assert status == 303 and content == ""
                    assert headers["Location"] == "/infrastructure?started=1"
                    with patch.object(
                        admin, "_backup_request",
                        side_effect=lambda kind: (
                            {"accepted": True, "overview": injected}
                            if kind == "backup-overview"
                            else (_ for _ in ()).throw(
                                admin.BackupRequestError(admin.BACKUP_REQUEST_ERRORS["media_not_ready"])
                            )
                        ),
                    ):
                        rejected, _, page = send(
                            http, "/infrastructure/run", cookie=cookie,
                            post={"csrf": session["csrf"]},
                        )
                        assert rejected == 409
                        assert "SSD и NAS ещё не подтверждены" in page
                    assert send(http, "/infrastructure/run", cookie=cookie,
                                post={"csrf": session["csrf"], "shell": "yes"})[0] == 400
                    admin.sessions.pop(sid)
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=5)
    print("[ОК] Storage/backup: YAML, timestamps, SSD/NAS, systemd, CSRF, HTML and API")


if __name__ == "__main__":
    main()
