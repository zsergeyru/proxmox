#!/usr/bin/env python3
"""Contract/unit checks deploy-guest без доступа к живому PVE."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/pve/deploy-guest.py"

spec = importlib.util.spec_from_file_location("deploy_guest", SOURCE)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def desired(kind: str = "vm"):
    effective = {
        "description": "Test guest",
        "type": kind,
        "name": "test-guest",
        "node": "pve",
        "network": {
            "bridge": "vmbr0",
            "gateway": "192.168.1.1",
            "ipv4": {"address": "192.168.3.1/16"},
        },
        "resources": {"disk": {"storage": "local-lvm", "size_gb": 24}},
    }
    return mod.Desired(301, Path("guests/301-test/guest.yaml"), {}, effective, "192.168.3.1", ())


def main() -> None:
    d = desired()

    assert mod.parse_tags("management-ssh;proxmox-deployer") == {
        "management-ssh",
        "proxmox-deployer",
    }
    assert mod.format_tags({"z", "a"}) == "a;z"

    desc = mod.service_description(d)
    assert "managed-by=proxmox-deployer" in desc
    assert "source-repo=zsergeyru/proxmox" in desc
    assert "manifest=guests/301-test/guest.yaml" in desc

    owned = {
        "tags": "foo;proxmox-deployer",
        "description": desc,
    }
    assert mod.has_ownership(owned, d)
    assert not mod.has_ownership({"tags": "foo", "description": desc}, d)

    final_tags = mod.desired_final_tags({"tags": "foo;deploy-incomplete"})
    assert final_tags == {"foo", "proxmox-deployer", "management-ssh"}
    incomplete = mod.desired_incomplete_tags({"tags": "foo"})
    assert "deploy-incomplete" in incomplete

    assert mod.parse_size_bytes("local-lvm:vm-301-disk-0,size=24G") == 24 * 1024**3
    assert mod.parse_size_bytes("local:301/rootfs.raw,size=1024M") == 1024 * 1024**2
    assert mod.parse_size_bytes("local-lvm:vm-301-disk-0") is None
    assert mod.storage_from_volume("local-lvm:vm-301-disk-0,size=24G") == "local-lvm"

    assert mod.update_kv_option("virtio=AA:BB,bridge=old", "bridge", "vmbr0") == "virtio=AA:BB,bridge=vmbr0"
    assert mod.update_kv_option("name=eth0", "ip", "192.168.3.1/16") == "name=eth0,ip=192.168.3.1/16"
    assert mod.expected_ipconfig(d) == "ip=192.168.3.1/16,gw=192.168.1.1"

    plan = mod.Plan(
        d,
        mod.Actual(False),
        [mod.PlanItem("PVE", "CREATE", "ONLINE", "test")],
    )
    assert plan.changes and not plan.blocked
    blocked = mod.Plan(
        d,
        mod.Actual(True),
        [mod.PlanItem("disk", "BLOCKED", "FORBIDDEN", "shrink")],
    )
    assert blocked.blocked

    parser = mod.build_arg_parser()
    args = parser.parse_args(["301"])
    assert args.vmid == 301 and args.apply is False
    args = parser.parse_args(["301", "--apply"])
    assert args.apply is True

    text = SOURCE.read_text(encoding="utf-8")
    assert "CERT_NONE" not in text
    assert "verify=False" not in text
    assert "subprocess" in text
    assert "DEPLOY_GUEST_SOURCE_REVISION" in text
    assert "deploy-incomplete" in text
    assert "management-ssh" in text
    assert "qm " not in text and "pct " not in text and "pvesh " not in text

    print("deploy-guest contract tests passed.")


if __name__ == "__main__":
    main()
