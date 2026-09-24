#!/usr/bin/env python3
"""Контрактные проверки guest resolver без доступа к PVE."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

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
    assert resolved.effective["management"] == ["ssh_identity", "project_repo_read"]
    assert resolved.effective["features"] == ["container-host"]
    assert resolved.effective["resources"]["cores"] == 2
    assert resolved.effective["network"]["ipv4"] == "192.168.3.11/16"
    assert resolved.effective["protection"] is True
    assert resolved.effective["pve_management"] is True
    assert "bootstrap" not in resolved.effective

    dhcp_guest = copy.deepcopy(source_311)
    dhcp_guest["network"] = {"ipv4": "dhcp"}
    resolved_dhcp = resolve_effective_guest(dhcp_guest, defaults)
    assert resolved_dhcp.management_ip is None
    assert resolved_dhcp.management_ip_source == "dhcp"
    assert resolved_dhcp.effective["network"]["ipv4"] == "dhcp"

    invalid_dhcp = copy.deepcopy(source_109)
    invalid_dhcp["network"] = {"ipv4": "dhcp/16"}
    expect_error(invalid_dhcp, defaults, "network.ipv4 должен быть без /prefix")

    source_schema = load_yaml(ROOT / "infrastructure/schemas/guest.schema.yaml")
    effective_schema = load_yaml(
        ROOT / "infrastructure/schemas/guest-effective.schema.yaml"
    )
    source_validator = Draft202012Validator(source_schema)
    effective_validator = Draft202012Validator(effective_schema)

    dhcp_source = copy.deepcopy(source_109)
    dhcp_source["network"] = {"ipv4": "dhcp"}
    assert not list(source_validator.iter_errors(dhcp_source))

    assert not list(
        effective_validator.iter_errors(resolved_dhcp.effective)
    )

    obsolete_bootstrap = copy.deepcopy(source_311)
    obsolete_bootstrap["bootstrap"] = ["git"]
    assert list(source_validator.iter_errors(obsolete_bootstrap))

    local_pve_override = copy.deepcopy(source_109)
    local_pve_override["protection"] = False
    local_pve_override["pve_management"] = False
    resolved_override = resolve_effective_guest(local_pve_override, defaults)
    assert resolved_override.effective["protection"] is False
    assert resolved_override.effective["pve_management"] is False

    print("Guest resolver contract tests passed.")


if __name__ == "__main__":
    main()
