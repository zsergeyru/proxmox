#!/usr/bin/env python3
"""Проверить формирование каталога Homarr без отдельного сервера и обновлений ПО."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager import homarr_catalog
from infra_manager.guest_catalog import deployable_guests


def test_catalog() -> None:
    catalog = homarr_catalog.build_catalog(ROOT)
    guests = catalog["guests"]
    assert catalog["schema_version"] == 1
    assert catalog["source"] == "git"
    assert catalog["pve_available"] is False
    assert catalog["actions_enabled"] is False
    assert len(guests) == len(deployable_guests(ROOT))
    assert {row["vmid"] for row in guests} == {
        guest.vmid for guest in deployable_guests(ROOT)
    }
    assert guests == sorted(guests, key=lambda guest: guest["vmid"])
    assert all(guest["state"] == "unknown" for guest in guests)
    assert all(guest["actions"] == [] for guest in guests)

    web_gateway = next(guest for guest in guests if guest["name"] == "web-gateway")
    assert any(
        "homarr:v2.5.2" in version["configured_image"].lower()
        for version in web_gateway["versions"]
    )
    assert all(version["installed_version"] is None for version in web_gateway["versions"])
    assert all(version["available_version"] is None for version in web_gateway["versions"])
    assert any(
        version["source"].endswith("immich_version")
        for guest in guests for version in guest["versions"]
    )

    # Нет фиксированного перечня VMID: новый гость должен появляться при чтении Git.
    mock_guest = SimpleNamespace(vmid=9910, name="new-service", role="test")
    with (
        patch.object(homarr_catalog, "deployable_guests", return_value=[mock_guest]),
        patch.object(homarr_catalog, "_load_provision", return_value={"guest_vmid": 9910}),
        patch.object(homarr_catalog, "guest_management_address", return_value="192.168.1.10"),
        patch.object(homarr_catalog, "portal_definitions", return_value=[]),
    ):
        fresh = homarr_catalog.build_catalog(ROOT)
    assert [guest["vmid"] for guest in fresh["guests"]] == [9910]


def test_pipeline() -> None:
    role = ROOT / "automation/ansible/roles/web_gateway"
    for path in [role / "tasks/main.yml", role / "tasks/sync.yml"]:
        tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert any(
            task.get("ansible.builtin.include_tasks") == "catalog.yml"
            for task in tasks
        ), path
    catalog_tasks = yaml.safe_load((role / "tasks/catalog.yml").read_text(encoding="utf-8"))
    assert any(
        "homarr-catalog" in task.get("ansible.builtin.command", {}).get("argv", [])
        for task in catalog_tasks
    )
    assert any(
        task.get("ansible.builtin.copy", {}).get("mode") == "0644"
        for task in catalog_tasks
    )
    assert all(
        "docker" not in str(task.get("ansible.builtin.command", {}))
        for task in catalog_tasks
    )

    compose = (role / "templates/docker-compose.yml.j2").read_text(encoding="utf-8")
    caddy = (role / "templates/Caddyfile.j2").read_text(encoding="utf-8")
    assert "/srv/homarr-catalog:ro" in compose
    assert ":8081 {" in caddy
    assert "file_server" in caddy
    assert "/apps.json" in caddy
    sync = (role / "tasks/sync.yml").read_text(encoding="utf-8")
    assert "home-apps.py" in sync
    assert "home-apps.py" in (role / "tasks/main.yml").read_text(encoding="utf-8")
    assert "8081:8081" not in compose
    assert '"8081:8081"' not in compose

    widget = json.loads(
        (ROOT / "infrastructure/guests/140-web-gateway/widgets/proxmox-guests.json")
        .read_text(encoding="utf-8")
    )
    assert widget["$schema"] == "homarr-custom-widget-v2"
    assert widget["sources"]["default"]["baseUrl"] == "http://caddy:8081"
    assert widget["sources"]["default"]["networkScope"] == "private"
    assert widget["sources"]["default"]["auth"] == "none"
    assert list(widget["requests"]) == ["inventory"]
    assert widget["requests"]["inventory"]["path"] == "/guests.json"
    assert "ActionButton" not in widget["template"]


def test_existing_portal_kept() -> None:
    assert (ROOT / "automation/ansible/roles/portal/tasks/main.yml").is_file()
    assert (ROOT / "scripts/infra-manager/infra_manager/portal_gateway.py").is_file()
    assert (
        ROOT / "infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/"
        "infra-manager-portal-gateway.service"
    ).is_file()
    assert not (ROOT / "scripts/infra-manager/infra_manager/homarr_api.py").exists()
    assert not (
        ROOT / "infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/"
        "infra-manager-homarr-api.service"
    ).exists()


if __name__ == "__main__":
    test_catalog()
    test_pipeline()
    test_existing_portal_kept()
    print("[ОК] Homarr: каталог Git, безопасный sync, внутренний Caddy, старый Homepage сохранён")
