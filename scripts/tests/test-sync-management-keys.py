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
        manifest="guests/301-test/guest.yaml",
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
        deployer = root / "pve_deployer_ed25519"
        make_key(deployer, "deployer")
        (registry / "pve_deployer_ed25519.pub").write_bytes((root / "pve_deployer_ed25519.pub").read_bytes())

        old_registry = mod.REGISTRY_DIR
        old_aggregate = mod.AGGREGATE_FILE
        old_deployer_registry = mod.DEPLOYER_REGISTRY_KEY
        old_deployer_pub = mod.DEPLOYER_PUB
        try:
            mod.REGISTRY_DIR = registry
            mod.AGGREGATE_FILE = registry / "management-authorized-keys"
            mod.DEPLOYER_REGISTRY_KEY = registry / "pve_deployer_ed25519.pub"
            mod.DEPLOYER_PUB = root / "pve_deployer_ed25519.pub"

            state = mod.load_registry()
            if list(state.files) != ["pve_deployer_ed25519.pub"]:
                fail("initial registry order is wrong")
            if not state.aggregate_changed:
                fail("missing aggregate must be reported as changed")

            guest = root / "301"
            make_key(guest, "301")
            (registry / "301.pub").write_bytes((root / "301.pub").read_bytes())
            state = mod.load_registry()
            if list(state.files) != ["pve_deployer_ed25519.pub", "301.pub"]:
                fail("VMID public key was not ordered after pve_deployer_ed25519.pub")
            if state.aggregate != state.files["pve_deployer_ed25519.pub"] + state.files["301.pub"]:
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
            "pve_deployer_ed25519.pub": b"key\n",
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


def test_untrusted_host_boundary() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("def prepare_participant(")
    end = source.index("\ndef persist_new_host_keys", start)
    block = source[start:end]
    if "if not known_host_exists(participant):" not in block:
        fail("prepare_participant must reject missing persistent host trust")
    if "scan_host_keys(participant)" in block or "temporary_known_hosts" in block:
        fail("bulk sync must not TOFU an unknown project participant")


def test_discovery_uses_project_state() -> None:
    old_load_api_token = mod.load_api_token
    old_api_get = mod.api_get
    old_find_manifest = mod.find_manifest
    old_read_yaml = mod.read_yaml
    old_participant_from_resource = mod.participant_from_resource
    resources = [
        {
            "vmid": 301,
            "name": "managed",
            "type": "qemu",
            "node": "pve",
            "status": "running",
            "tags": "proxmox-deployer",
        },
        {
            "vmid": 302,
            "name": "external",
            "type": "qemu",
            "node": "pve",
            "status": "running",
            "tags": "unrelated",
        },
    ]
    try:
        mod.load_api_token = lambda: ("token", "secret")
        mod.api_get = lambda *args, **kwargs: resources
        mod.find_manifest = lambda vmid: Path("guests/301-test/guest.yaml") if vmid == 301 else None
        mod.read_yaml = lambda path: {"profile": "vm"}
        mod.participant_from_resource = lambda resource, defaults: participant(int(resource["vmid"]))

        found = mod.discover_participants({"defaults": {"node": "pve"}})
        if [item.vmid for item in found] != [301]:
            fail("participant discovery must come from project manifests, not a Proxmox participation tag")

        resources[0]["tags"] = ""
        try:
            mod.discover_participants({"defaults": {"node": "pve"}})
        except mod.SyncError:
            pass
        else:
            fail("project guest without proxmox-deployer ownership tag was accepted")
    finally:
        mod.load_api_token = old_load_api_token
        mod.api_get = old_api_get
        mod.find_manifest = old_find_manifest
        mod.read_yaml = old_read_yaml
        mod.participant_from_resource = old_participant_from_resource


def test_source_contract() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    if "management-" + "ssh" in text:
        fail("legacy participation tag remains in sync runtime")
    if "pve_" + "guest_ed25519" in text:
        fail("legacy PVE guest key name remains in sync runtime")
    if "deployer" + ".pub" in text:
        fail("legacy deployer registry filename remains in sync runtime")
    if "pve_deployer_ed25519.pub" not in text:
        fail("canonical PVE deployer registry key is not used")


def test_helpers() -> None:
    if mod.parse_tags("foo;proxmox-deployer;bar") != {"foo", "proxmox-deployer", "bar"}:
        fail("PVE tag parser is wrong")
    if mod.os_release_id(b'NAME="Debian GNU/Linux"\nID=debian\n') != "debian":
        fail("Debian os-release parser is wrong")
    if mod.known_host_lookup_name(participant()) != "192.168.3.1":
        fail("default SSH known_hosts lookup name is wrong")
    p = mod.Participant(301, "test", "qemu", "pve", "running", "192.168.3.1", "root", 2222, "guests/301-test/guest.yaml")
    if mod.known_host_lookup_name(p) != "[192.168.3.1]:2222":
        fail("non-default SSH known_hosts lookup name is wrong")


def main() -> None:
    test_authorized_keys_block()
    test_registry()
    test_remote_catalog_policy()
    test_untrusted_host_boundary()
    test_discovery_uses_project_state()
    test_source_contract()
    test_helpers()
    print("sync-management-keys unit tests passed.")


if __name__ == "__main__":
    main()
