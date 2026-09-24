#!/usr/bin/env python3
"""Unit and contract checks for infra-manager template operations."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.template import _template_failures  # noqa: E402
from infra_manager.template_build import _packer_inputs  # noqa: E402
from infra_manager.template_verify import _validate_cloud_status  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    packer_client = SimpleNamespace(
        url="https://pve.example:8006",
        token_id="root@pam!infra-manager",
        token_secret="pve-secret",
    )
    packer_env, packer_args = _packer_inputs(
        {"BASE": "1"},
        client=packer_client,
        node="pve",
        iso_url="https://example.invalid/debian.iso",
        iso_checksum="sha512:deadbeef",
        build_password="build-secret",
    )
    if packer_env.get("PKR_VAR_proxmox_token") != "pve-secret":
        fail("Packer API token was not passed through PKR_VAR_proxmox_token")
    if packer_env.get("PKR_VAR_build_password") != "build-secret":
        fail("Packer build password was not passed through PKR_VAR_build_password")
    joined_packer_args = " ".join(packer_args)
    if "pve-secret" in joined_packer_args or "build-secret" in joined_packer_args:
        fail("A Packer secret leaked into command-line arguments")
    if any(
        arg.startswith("-var=proxmox_token=")
        or arg.startswith("-var=build_password=")
        for arg in packer_args
    ):
        fail("Secret Packer variables are still passed via -var")

    valid_template = {
        "template": 1,
        "name": "tpl-debian13",
        "description": "template-version=8",
        "protection": 1,
        "scsihw": "virtio-scsi-single",
        "agent": "1",
        "ide0": "local-lvm:cloudinit",
        "ciuser": "root",
        "ipconfig0": "ip=dhcp",
        "ciupgrade": 0,
    }
    if _template_failures(valid_template):
        fail("A valid template 9000 did not pass Python validation")

    invalid_template = dict(valid_template)
    invalid_template["ide0"] = ""
    if "ide0=<cloudinit отсутствует>" not in _template_failures(invalid_template):
        fail("Template validation did not detect missing Cloud-Init")

    _validate_cloud_status(
        '{"status":"done","init":{},"init-local":{},'
        '"modules-config":{},"modules-final":{}}'
    )

    print("infra-manager template checks passed.")


if __name__ == "__main__":
    main_test()
