#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/pve/sync-management-keys.py"

spec = importlib.util.spec_from_file_location("sync_management_keys", SOURCE)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load sync-management-keys.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def fail(message: str) -> None:
    raise SystemExit(f"sync-management-keys test failed: {message}")


def make_key(path: Path, comment: str) -> None:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
    )


def participant(vmid: int = 301):
    return mod.Participant(
        vmid=vmid,
        name="test",
        kind="qemu",
        node="pve",
        status="running",
        address="192.168.3.1",
        user="root",
        port=22,
        manifest=None,
    )


def test_authorized_keys_block() -> None:
    p = participant()
    aggregate = b"ssh-ed25519 AAAATEST deployer\nssh-ed25519 BBBBTEST manager\n"
    original = b"ssh-ed25519 PERSONAL alice\n"
    rendered = mod.render_authorized_keys(original, aggregate, p)
    expected = (
        b"ssh-ed25519 PERSONAL alice\n"
        b"# BEGIN PROXMOX-MANAGEMENT-KEYS\n"
        + aggregate
        + b"# END PROXMOX-MANAGEMENT-KEYS\n"
    )
    if rendered != expected:
        fail("managed authorized_keys block was not appended while preserving personal key")

    replaced = mod.render_authorized_keys(
        b"before\n# BEGIN PROXMOX-MANAGEMENT-KEYS\nold\n# END PROXMOX-MANAGEMENT-KEYS\nafter\n",
        aggregate,
        p,
    )
    if not replaced.startswith(b"before\n# BEGIN PROXMOX-MANAGEMENT-KEYS\n"):
        fail("managed block replacement damaged prefix")
    if not replaced.endswith(b"# END PROXMOX-MANAGEMENT-KEYS\nafter\n"):
        fail("managed block replacement damaged suffix")
    if b"old\n" in replaced:
        fail("old managed key material remained after replacement")

    try:
        mod.render_authorized_keys(
            b"# BEGIN PROXMOX-MANAGEMENT-KEYS\nmissing-end\n",
            aggregate,
            p,
        )
    except mod.UnsafeRemoteState:
        pass
    else:
        fail("broken managed block was accepted")


def test_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        registry = root / "public-keys"
        registry.mkdir()
        deployer = root / "deployer"
        make_key(deployer, "deployer")
        (registry / "deployer.pub").write_bytes((root / "deployer.pub").read_bytes())

        old_registry = mod.REGISTRY_DIR
        old_aggregate = mod.AGGREGATE_FILE
        old_deployer_registry = mod.DEPLOYER_REGISTRY_KEY
        old_deployer_pub = mod.DEPLOYER_PUB
        try:
            mod.REGISTRY_DIR = registry
            mod.AGGREGATE_FILE = registry / "management-authorized-keys"
            mod.DEPLOYER_REGISTRY_KEY = registry / "deployer.pub"
            mod.DEPLOYER_PUB = root / "deployer.pub"

            state = mod.load_registry()
            if list(state.files) != ["deployer.pub"]:
                fail("initial registry order is wrong")
            if not state.aggregate_changed:
                fail("missing aggregate must be reported as changed")

            guest = root / "301"
            make_key(guest, "301")
            (registry / "301.pub").write_bytes((root / "301.pub").read_bytes())
            state = mod.load_registry()
            if list(state.files) != ["deployer.pub", "301.pub"]:
                fail("VMID public key was not ordered after deployer.pub")
            if state.aggregate != state.files["deployer.pub"] + state.files["301.pub"]:
                fail("deterministic aggregate is wrong")

            (registry / "311.pub").write_bytes((registry / "301.pub").read_bytes())
            try:
                mod.load_registry()
            except mod.SyncError:
                pass
            else:
                fail("duplicate public-key fingerprint was accepted")
            (registry / "311.pub").unlink()

            (registry / "unexpected.txt").write_text("x", encoding="utf-8")
            try:
                mod.load_registry()
            except mod.SyncError:
                pass
            else:
                fail("unexpected registry file was accepted")
        finally:
            mod.REGISTRY_DIR = old_registry
            mod.AGGREGATE_FILE = old_aggregate
            mod.DEPLOYER_REGISTRY_KEY = old_deployer_registry
            mod.DEPLOYER_PUB = old_deployer_pub


def test_remote_catalog_policy() -> None:
    p = participant()
    safe = mod.RemoteState(
        os_release=b"ID=debian\n",
        auth_exists=False,
        authorized_keys=b"",
        catalog_exists=True,
        catalog_files={
            "deployer.pub": b"key\n",
            "301.pub": b"key2\n",
            "management-authorized-keys": b"keys\n",
        },
    )
    mod.validate_remote_catalog(safe, p)

    unsafe = mod.RemoteState(
        os_release=b"ID=debian\n",
        auth_exists=False,
        authorized_keys=b"",
        catalog_exists=True,
        catalog_files={"notes.txt": b"do not delete me"},
    )
    try:
        mod.validate_remote_catalog(unsafe, p)
    except mod.UnsafeRemoteState:
        pass
    else:
        fail("unknown guest catalog file was accepted")


def test_helpers() -> None:
    if mod.parse_tags("foo;management-ssh;bar") != {"foo", "management-ssh", "bar"}:
        fail("PVE tag parser is wrong")
    if mod.os_release_id(b'NAME="Debian GNU/Linux"\nID=debian\n') != "debian":
        fail("Debian os-release parser is wrong")
    if mod.known_host_lookup_name(participant()) != "192.168.3.1":
        fail("default SSH known_hosts lookup name is wrong")
    p = mod.Participant(301, "test", "qemu", "pve", "running", "192.168.3.1", "root", 2222, None)
    if mod.known_host_lookup_name(p) != "[192.168.3.1]:2222":
        fail("non-default SSH known_hosts lookup name is wrong")


def main() -> None:
    test_authorized_keys_block()
    test_registry()
    test_remote_catalog_policy()
    test_helpers()
    print("sync-management-keys unit tests passed.")


if __name__ == "__main__":
    main()
