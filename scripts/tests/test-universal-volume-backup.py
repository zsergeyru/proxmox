#!/usr/bin/env python3
"""Контракт безопасного универсального резервирования LVM-томов и страницы 910."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager import backup_controller, backup_overview, volume_backup
from infra_manager.common import InfraManagerError
from infra_manager.operation_store import _atomic_json

spec = importlib.util.spec_from_file_location(
    "pve_volume_backup_host", ROOT / "scripts/infra-manager/host/backup-export.py",
)
assert spec and spec.loader
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def refused(fn, kinds=(InfraManagerError, host.ExportError)) -> None:
    try:
        fn()
    except kinds:
        return
    raise AssertionError("Опасный или некорректный план должен быть отклонён")


def policy() -> dict:
    return yaml.safe_load(
        (ROOT / "infrastructure/hosts/pve/backup.yaml").read_text(encoding="utf-8")
    )


def test_manifest() -> None:
    document = policy()
    jobs = volume_backup.load_jobs(ROOT, document)
    assert [x["id"] for x in jobs] == ["infra_state", "gogs_data"]
    assert jobs[0] == {
        "id": "infra_state", "mountpoint": "/mnt/bindmounts/infra-state",
        "consumers": [140, 340],
    }
    assert jobs[1]["consumers"] == [310]
    assert all(x["id"] != "photo_archive" for x in jobs)
    assert document["volume_backups"]["defaults"]["schedule"] == "daily"
    for failure in (
        [{"id": "unknown"}],
        [{"id": "gogs_data"}, {"id": "gogs_data"}],
        [{"id": "../escape"}],
        [{"id": "infra_state", "mountpoint": "/"}],
    ):
        wrong = policy()
        wrong["volume_backups"]["volumes"] = failure
        refused(lambda: volume_backup.load_jobs(ROOT, wrong))
    wrong = policy()
    wrong["volume_backups"]["defaults"]["method"] = "unsafe"
    refused(lambda: volume_backup.load_jobs(ROOT, wrong))
    wrong = policy()
    wrong["volume_backups"]["defaults"]["destinations"] = ["fast-ssd", "fast-ssd"]
    refused(lambda: volume_backup.load_jobs(ROOT, wrong))


def test_host_guards() -> None:
    for bad in (
        {},
        {"id": "../etc", "mountpoint": "/mnt/bindmounts/../../etc", "consumers": []},
        {"id": "gogs_data", "mountpoint": "/mnt/bindmounts/infra-state", "consumers": []},
    ):
        refused(lambda: host._volume_contract(bad))
    calls = []
    def run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["pct", "list"]:
            return SimpleNamespace(returncode=0, stdout="VMID Status Name\n310 running gogs\n410 running ai\n")
        if cmd[:2] == ["pct", "config"]:
            path = ("/mnt/bindmounts/gogs-data/310-gogs" if cmd[2] == "310"
                    else "/mnt/bindmounts/other/410")
            return SimpleNamespace(
                returncode=0, stdout=f"mp0: {path},mp=/var/lib/gogs\n"
            )
        raise AssertionError(cmd)
    with patch.object(host.subprocess, "run", side_effect=run):
        assert host._consumers_on_host(Path("/mnt/bindmounts/gogs-data"), {310}) == [310]
        refused(lambda: host._consumers_on_host(
            Path("/mnt/bindmounts/gogs-data"), {410},
        ))
    assert calls


def test_snapshot_cleanup() -> None:
    original_temp = tempfile.TemporaryDirectory
    def make_temp(*_args, **_kwargs):
        return original_temp()
    calls = []
    state = {"running": True}
    def run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["pct", "status"]:
            return SimpleNamespace(
                returncode=0,
                stdout="status: running\n" if state["running"] else "status: stopped\n",
            )
        if command[:2] == ["pct", "shutdown"]:
            state["running"] = False
        if command[:2] == ["pct", "start"]:
            state["running"] = True
        return SimpleNamespace(returncode=0, stdout="")
    volume = {
        "id": "gogs_data", "mountpoint": "/mnt/bindmounts/gogs-data",
        "consumers": [310],
    }
    with (
        patch.object(host, "_volume_contract", return_value=(
            "gogs_data", Path(volume["mountpoint"]), Path("/dev/pve/gogs_data"), {310},
        )),
        patch.object(host, "_thin_pool_ready"),
        patch.object(host, "_consumers_on_host", return_value=[310]),
        patch.object(host, "_consistent_window", return_value=contextlib.nullcontext()),
        patch.object(host.tempfile, "TemporaryDirectory", side_effect=make_temp),
        patch.object(host.subprocess, "run", side_effect=run),
    ):
        host.stream_volume(volume)
    actions = [x[0] for x in calls]
    for item in ("lvcreate", "mount", "tar", "umount", "lvremove"):
        assert item in actions
    assert actions.index("lvcreate") < actions.index("mount") < actions.index("tar")
    assert actions.index("tar") < actions.index("umount") < actions.index("lvremove")
    assert calls.index(["pct", "shutdown", "310", "--timeout", "40"]) < next(
        i for i, x in enumerate(calls) if x[0] == "lvcreate"
    )
    assert next(i for i, x in enumerate(calls) if x[:2] == ["pct", "start"]) < actions.index("tar")

    failure_calls = []
    failure_state = {"running": True}
    def failed(command, **_kwargs):
        failure_calls.append(command)
        code = 1 if command[0] == "lvcreate" else 0
        if command[:2] == ["pct", "shutdown"]:
            failure_state["running"] = False
        if command[:2] == ["pct", "start"]:
            failure_state["running"] = True
        return SimpleNamespace(
            returncode=code,
            stdout=("status: running\n" if failure_state["running"]
                    else "status: stopped\n") if command[:2] == ["pct", "status"] else "",
        )
    with (
        patch.object(host, "_volume_contract", return_value=(
            "gogs_data", Path(volume["mountpoint"]), Path("/dev/pve/gogs_data"), {310},
        )),
        patch.object(host, "_thin_pool_ready"),
        patch.object(host, "_consumers_on_host", return_value=[310]),
        patch.object(host, "_consistent_window", return_value=contextlib.nullcontext()),
        patch.object(host.tempfile, "TemporaryDirectory", side_effect=make_temp),
        patch.object(host.subprocess, "run", side_effect=failed),
    ):
        refused(lambda: host.stream_volume(volume))
    assert any(x[:2] == ["pct", "start"] for x in failure_calls)
    assert not any(x[0] == "tar" for x in failure_calls)


def test_records_and_restic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        store = SimpleNamespace(settings=state)
        job = {"id": "gogs_data", "mountpoint": "/mnt/bindmounts/gogs-data",
               "consumers": [310]}
        today = datetime.now(UTC).date().isoformat()
        assert volume_backup.due(store, "gogs_data", ["fast-ssd"])
        assert backup_overview._volume_record(
            store, "gogs_data", datetime.now(UTC),
        )["state"] == "missing"
        actions = []
        def remote(_node, action, _data=b""):
            actions.append(("remote", action))
            if action == "volume-repositories":
                return SimpleNamespace(stdout=b'{"fast-ssd":"missing"}')
            return SimpleNamespace(stdout=b"")
        with (
            patch.object(backup_controller, "_remote", side_effect=remote),
            patch.object(backup_controller, "_restic", side_effect=lambda
                         _node, _dst, _key, argv, **kwargs:
                         actions.append(("restic", argv[0], kwargs.get("volume_id")))),
            patch.object(backup_controller, "_stream_volume", side_effect=lambda
                         *_args: actions.append(("stream", "gogs_data"))),
            patch.object(backup_controller, "volume_due", return_value=True),
        ):
            assert backup_controller._backup_volumes(
                "pve", {"fast-ssd": "UUID"}, state / "password", store, [job],
            ) == {}
        assert ("remote", "volume-prepare") in actions
        assert ("restic", "init", "gogs_data") in actions
        assert ("stream", "gogs_data") in actions
        saved = json.loads((state / "backup-volume-gogs_data.json").read_text())
        assert saved["destination_dates"] == {"fast-ssd": today}
        assert not volume_backup.due(store, "gogs_data", ["fast-ssd"])
        assert volume_backup.due(store, "gogs_data", ["fast-ssd", "nas-backup"])
        overview = backup_overview._volume_record(store, "gogs_data", datetime.now(UTC))
        assert overview["state"] == "partial"
        assert overview["destinations"] == ["fast-ssd"]
        assert overview["restore_tested"] is False
        _atomic_json(state / "backup-volume-gogs_data.json", {
            **saved,
            "destination_dates": {"fast-ssd": today, "nas-backup": today},
        })
        assert backup_overview._volume_record(
            store, "gogs_data", datetime.now(UTC),
        )["state"] == "verified"


def main() -> None:
    test_manifest()
    test_host_guards()
    test_snapshot_cleanup()
    test_records_and_restic()
    print("[ОК] Универсальные тома, согласование LXC, Restic и веб-статус")


if __name__ == "__main__":
    main()
