#!/usr/bin/env python3
"""Защитные проверки регистрации NFS через Proxmox API без доступа к PVE."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import pve_backup_nas as nas
from infra_manager.common import InfraManagerError


class Client:
    def __init__(self, *, existing: dict | None = None):
        self.existing = existing
        self.created = []

    def data(self, endpoint):
        if endpoint == "/storage":
            return [{"storage": nas.STORAGE_ID}] if self.existing else []
        if endpoint == f"/storage/{nas.STORAGE_ID}":
            return self.existing
        raise AssertionError(endpoint)

    def request_data(self, method, endpoint, *, form):
        self.created.append((method, endpoint, form))
        return None


def refuses(fn):
    try:
        fn()
    except InfraManagerError:
        return
    raise AssertionError("Должен быть отказ при подмене NAS")


def test_declaration_and_native_create():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = root / "infrastructure/hosts/pve/backup.yaml"
        manifest.parent.mkdir(parents=True)
        config = {
            "schema_version": 1,
            "destinations": [{"id": "nas-backup",
                              "mountpoint": nas.MOUNTPOINT,
                              "source": "192.168.9.20:/exports/pve"}],
        }
        manifest.write_text(yaml.safe_dump(config), encoding="utf-8")
        with (
            patch.object(nas, "PATHS", SimpleNamespace(repo_root=root)),
            patch.object(nas, "_mount_verified", return_value=True) as verified,
        ):
            client = Client()
            assert nas.reconcile_backup_nas(client, "pve")
            assert client.created == [("POST", "/storage", {
                "type": "nfs", "storage": "nas-backup", "server": "192.168.9.20",
                "export": "/exports/pve", "content": "backup", "nodes": "pve",
            })]
            verified.assert_called_once_with("pve", "192.168.9.20", "/exports/pve")
            good = {"type": "nfs", "server": "192.168.9.20",
                    "export": "/exports/pve"}
            client = Client(existing=good)
            assert nas.reconcile_backup_nas(client, "pve")
            assert not client.created
            client = Client(existing=dict(good, server="192.168.9.99"))
            refuses(lambda: nas.reconcile_backup_nas(client, "pve"))
            config["destinations"][0]["source"] = None
            manifest.write_text(yaml.safe_dump(config), encoding="utf-8")
            assert nas.reconcile_backup_nas(Client(), "pve") is False
            config["destinations"][0]["source"] = "192.168.9.20:/../wrong"
            manifest.write_text(yaml.safe_dump(config), encoding="utf-8")
            refuses(nas._declared)


if __name__ == "__main__":
    test_declaration_and_native_create()
    print("[ОК] NAS через PVE API: только подтверждённый NFSv4 и без установок")
