#!/usr/bin/env python3
"""Контрактные проверки guest resolver без доступа к PVE."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "guests"))

from resolver import GuestConfigError, resolve_effective_guest  # noqa: E402


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def expect_error(source: dict, defaults: dict, needle: str) -> None:
    try:
        resolve_effective_guest(source, defaults)
    except GuestConfigError as exc:
        assert needle in str(exc), (needle, str(exc))
    else:
        raise AssertionError(f"Expected GuestConfigError containing {needle!r}")


def main() -> None:
    defaults = load_yaml(ROOT / "infrastructure/guests/defaults.yaml")
    source_311 = load_yaml(ROOT / "infrastructure/guests/311-dev-services/guest.yaml")
    source_109 = load_yaml(ROOT / "infrastructure/guests/109-network-gateway/guest.yaml")

    resolved = resolve_effective_guest(source_311, defaults)
    assert resolved.bootstrap_capabilities == ("git", "docker", "ansible")
    assert resolved.effective["management"] == ["ssh_identity", "project_repo_read"]
    assert resolved.effective["features"] == ["container-host"]
    assert resolved.effective["resources"]["cores"] == 2
    assert resolved.effective["network"]["ipv4"] == "192.168.3.11/16"
    assert resolved.effective["protection"] is True
    assert resolved.effective["pve_management"] is True

    dhcp_guest = copy.deepcopy(source_311)
    dhcp_guest["network"] = {"ipv4": "dhcp"}
    resolved_dhcp = resolve_effective_guest(dhcp_guest, defaults)
    assert resolved_dhcp.management_ip is None
    assert resolved_dhcp.management_ip_source == "dhcp"
    assert resolved_dhcp.effective["network"]["ipv4"] == "dhcp"

    dhcp = copy.deepcopy(source_109)
    dhcp["network"] = {"ipv4": "dhcp"}
    resolved_dhcp = resolve_effective_guest(dhcp, defaults)
    assert resolved_dhcp.effective["network"]["ipv4"] == "dhcp"
    assert resolved_dhcp.management_ip is None
    assert resolved_dhcp.management_ip_source == "dhcp"

    invalid_dhcp = copy.deepcopy(source_109)
    invalid_dhcp["network"] = {"ipv4": "dhcp/16"}
    expect_error(invalid_dhcp, defaults, "некорректный network.ipv4")

    local_pve_override = copy.deepcopy(source_109)
    local_pve_override["protection"] = False
    local_pve_override["pve_management"] = False
    resolved_override = resolve_effective_guest(local_pve_override, defaults)
    assert resolved_override.effective["protection"] is False
    assert resolved_override.effective["pve_management"] is False

    no_bootstrap = copy.deepcopy(source_311)
    no_bootstrap.pop("bootstrap")
    resolved_no_bootstrap = resolve_effective_guest(no_bootstrap, defaults)
    assert resolved_no_bootstrap.bootstrap_capabilities == ()
    assert resolved_no_bootstrap.effective["bootstrap"] == []

    ansible_only = copy.deepcopy(source_109)
    ansible_only["bootstrap"] = ["ansible"]
    assert resolve_effective_guest(ansible_only, defaults).bootstrap_capabilities == (
        "ansible",
    )

    duplicate = copy.deepcopy(source_311)
    duplicate["bootstrap"] = ["git", "git"]
    expect_error(duplicate, defaults, "не должен содержать дубликаты")

    unknown = copy.deepcopy(source_311)
    unknown["bootstrap"] = ["git", "unknown"]
    expect_error(unknown, defaults, "неизвестные элементы bootstrap")

    empty = copy.deepcopy(source_311)
    empty["bootstrap"] = []
    expect_error(empty, defaults, "должен быть непустым list")

    plain_lxc_defaults = copy.deepcopy(defaults)
    plain_lxc_defaults["profiles"]["debian-lxc"] = {
        "type": "lxc",
        "ostemplate": "local:vztmpl/debian-13-standard",
    }
    docker_without_feature = copy.deepcopy(source_311)
    docker_without_feature["profile"] = "debian-lxc"
    docker_without_feature["bootstrap"] = ["docker"]
    expect_error(
        docker_without_feature,
        plain_lxc_defaults,
        "требует feature 'container-host'",
    )

    print("Guest resolver contract tests passed.")


if __name__ == "__main__":
    main()
