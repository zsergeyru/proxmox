#!/usr/bin/env python3
"""Локальные проверки каталога Homarr и защищённых границ API."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import homarr_api, homarr_inventory
from infra_manager.common import InfraManagerError


def check_inventory() -> None:
    resources = [
        {"vmid": 100, "name": "home-assistant", "type": "qemu", "status": "running"},
        {"vmid": 140, "name": "web-gateway", "type": "lxc", "status": "running"},
        {"vmid": 340, "name": "photo-archive", "type": "lxc", "status": "stopped"},
    ]
    result = homarr_inventory.guest_inventory(ROOT, resources=resources)
    index = {row["vmid"]: row for row in result["guests"]}
    assert index[100]["managed"] is False
    assert index[100]["actions"] == []
    assert index[140]["state"] == "running"
    assert index[140]["managed"] is True
    assert any("Homarr" in x["configured_image"] for x in index[140]["versions"]) or any(
        "homarr" in x["configured_image"] for x in index[140]["versions"]
    )
    assert all(x["installed_version"] is None for x in index[140]["versions"])
    assert all(x["available_version"] is None for x in index[140]["versions"])
    assert index[340]["state"] == "stopped"
    assert index[340]["managed"] is True
    assert any("immich" in x["configured_image"] for x in index[340]["versions"])

    unknown = homarr_inventory.guest_inventory(ROOT, resources=None, pve_error="Проверка не прошла")
    unavailable = {row["vmid"]: row for row in unknown["guests"]}
    assert unavailable[140]["state"] == "unknown"
    assert unknown["pve_available"] is False
    # Добавление VMID без provision.yaml не должно делать объект управляемым.
    bad = homarr_inventory.guest_inventory(
        ROOT, resources=[{"vmid": 9901, "type": "lxc", "name": "unknown", "status": "running"}]
    )
    assert next(x for x in bad["guests"] if x["vmid"] == 9901)["actions"] == []
    mismatched = homarr_inventory.guest_inventory(
        ROOT, resources=[{"vmid": 140, "type": "lxc", "name": "other", "status": "running"}]
    )
    assert next(x for x in mismatched["guests"] if x["vmid"] == 140)["state"] == "mismatch"
    assert next(x for x in mismatched["guests"] if x["vmid"] == 140)["actions"] == []


def check_tokens() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        homarr_api.initialize_keys(root)
        read = (root / "read-token").read_text().strip()
        action = (root / "action-token").read_text().strip()
        assert read != action and len(read) == len(action) == 64
        homarr_api.initialize_keys(root)
        assert (root / "read-token").read_text().strip() == read
        assert homarr_api._key_matches("Bearer " + read, root / "read-token")
        assert not homarr_api._key_matches("Bearer " + read, root / "action-token")
        assert not homarr_api._key_matches("Bearer nonsense", root / "read-token")


def check_clients() -> None:
    assert homarr_api.allowed_client("127.0.0.1", ROOT)
    assert homarr_api.allowed_client("192.168.1.40", ROOT)
    assert not homarr_api.allowed_client("192.168.1.41", ROOT)
    assert not homarr_api.allowed_client("203.0.113.1", ROOT)


def check_invalid_job() -> None:
    for value in ("../x", "bad", "a" * 33):
        try:
            homarr_api._read_job(value)
        except InfraManagerError:
            continue
        raise AssertionError("Недопустимый идентификатор задания принят")


def check_widget() -> None:
    path = ROOT / "infrastructure/guests/140-web-gateway/widgets/proxmox-guests.json"
    widget = json.loads(path.read_text(encoding="utf-8"))
    assert widget["$schema"] == "homarr-custom-widget-v2"
    assert widget["sources"]["default"]["auth"] == "bearer"
    assert widget["sources"]["default"]["networkScope"] == "private"
    assert widget["sources"]["actions"]["auth"] == "bearer"
    assert widget["requests"]["inventory"]["path"] == "/v1/guests"
    assert widget["requests"]["job"]["trigger"] == "manual"
    for operation in ("status", "sync", "repair", "deploy"):
        action = widget["requests"][operation + "Guest"]
        assert action["kind"] == "action" and action["method"] == "POST"
        assert action["permission"] == "full"
        assert action["confirmation"]
    assert "data.inventory?.guests" in widget["template"]
    assert "data.inventory?.actions_enabled" in widget["template"]
    assert "ActionButton" in widget["template"]
    assert "homarr" in yaml.safe_load(
        (ROOT / "infrastructure/guests/140-web-gateway/provision.yaml").read_text()
    )["web_gateway"]


def check_portal_retained() -> None:
    assert (ROOT / "automation/ansible/roles/portal/tasks/main.yml").is_file()
    assert (ROOT / "scripts/infra-manager/infra_manager/portal_gateway.py").is_file()
    assert (
        ROOT / "infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/"
        "infra-manager-portal-gateway.service"
    ).is_file()


if __name__ == "__main__":
    check_inventory()
    check_tokens()
    check_clients()
    check_invalid_job()
    check_widget()
    check_portal_retained()
    print("[ОК] API Homarr: гостевой каталог, два ключа, права, виджет и сохранённый Homepage")
