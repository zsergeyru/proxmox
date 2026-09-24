#!/usr/bin/env python3
"""Unit and contract checks for infra-manager setup."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.host_setup import (  # noqa: E402
    BASE_PACKAGES,
    PVE_API_ENV,
    HostSetup,
)
from infra_manager.runtime_setup import (  # noqa: E402
    PACKER_VERSION,
    RuntimeSetup,
)
from infra_manager.settings import PATHS, SETTINGS  # noqa: E402
from infra_manager.setup import Setup  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    if "python3" not in BASE_PACKAGES:
        fail("python3 is missing from the setup contract")
    if "python3-yaml" not in BASE_PACKAGES:
        fail("python3-yaml is missing from the setup contract")

    if PATHS.pve_api_env != Path("/etc/infra-manager/secrets/pve-api.env"):
        fail("The canonical PVE credential path has an unexpected value")
    if PATHS.ca_bundle != Path("/etc/infra-manager/ca/ca-bundle.crt"):
        fail("The canonical CA bundle path has an unexpected value")
    if PATHS.opentofu_input != Path("/var/lib/infra-manager/opentofu/guests.json"):
        fail("The canonical OpenTofu input path has an unexpected value")
    if SETTINGS.default_project_branch != "main":
        fail("The default project branch must be main")
    if PACKER_VERSION != SETTINGS.packer_version:
        fail("setup does not use the Packer version from shared settings")
    if PVE_API_ENV != PATHS.pve_api_env:
        fail("setup does not use the PVE credential path from shared settings")

    compose = Setup.compose("ps")
    if compose[:2] != ["docker", "compose"] or compose[-1] != "ps":
        fail(f"Unexpected Docker Compose command: {compose!r}")
    if not issubclass(Setup, HostSetup) or not issubclass(Setup, RuntimeSetup):
        fail("Setup does not combine the host and runtime setup stages")

    print("infra-manager setup checks passed.")


if __name__ == "__main__":
    main_test()
