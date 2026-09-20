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
        "management": ["ssh_identity", "project_repo_read"],
    }
    return mod.Desired(301, Path("guests/301-test/guest.yaml"), {}, effective, "192.168.3.1", ())


def main() -> None:
    d = desired()

    assert mod.parse_tags("proxmox-deployer;custom") == {
        "proxmox-deployer",
        "custom",
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
    assert final_tags == {"foo", "proxmox-deployer"}
    incomplete = mod.desired_incomplete_tags({"tags": "foo"})
    assert "deploy-incomplete" in incomplete
    assert mod.management_requested(d, "ssh_identity")
    assert mod.management_requested(d, "project_repo_read")
    assert not mod.management_requested(d, "unknown")

    assert mod.parse_size_bytes("local-lvm:vm-301-disk-0,size=24G") == 24 * 1024**3
    assert mod.parse_size_bytes("local:301/rootfs.raw,size=1024M") == 1024 * 1024**2
    assert mod.parse_size_bytes("local-lvm:vm-301-disk-0") is None
    assert mod.storage_from_volume("local-lvm:vm-301-disk-0,size=24G") == "local-lvm"

    assert mod.update_kv_option("virtio=AA:BB,bridge=old", "bridge", "vmbr0") == "virtio=AA:BB,bridge=vmbr0"
    assert mod.update_kv_option("name=eth0", "ip", "192.168.3.1/16") == "name=eth0,ip=192.168.3.1/16"
    assert mod.expected_ipconfig(d) == "ip=192.168.3.1/16,gw=192.168.1.1"
    assert mod.preserve_boolean_suboptions("1,fstrim_cloned_disks=1", True) == "1,fstrim_cloned_disks=1"
    assert mod.preserve_boolean_suboptions("1,fstrim_cloned_disks=1", False) == "0,fstrim_cloned_disks=1"

    lxc = desired("lxc")
    lxc.effective["lxc"] = {"features": {"keyctl": True, "nesting": True}}
    assert set(mod.expected_lxc_features(lxc, "fuse=1,keyctl=0,nesting=1").split(",")) == {
        "fuse=1", "keyctl=1", "nesting=1"
    }

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
    assert "PROJECT_GIT_BEGIN" in text and "PROJECT_GIT_END" in text
    state_start = text.index("def project_repo_state(")
    state_end = text.index("\ndef bootstrap_check", state_start)
    state_block = text[state_start:state_end]
    assert "command -v git" in state_block
    assert "successfully authenticated" in state_block
    assert "git ls-remote" in state_block
    assert "CERT_NONE" not in text
    assert "verify=False" not in text
    assert "subprocess" in text
    assert "DEPLOY_GUEST_SOURCE_REVISION" in text
    load_start = text.index("def load_desired(")
    load_end = text.index("\ndef load_local_config", load_start)
    assert "start_after_deploy" in text[load_start:load_end]
    assert "deploy-incomplete" in text
    assert "management-" + "ssh" not in text
    assert "qm " not in text and "pct " not in text and "pvesh " not in text

    print("deploy-guest contract tests passed.")


if __name__ == "__main__":
    main()
