#!/usr/bin/env python3
"""Точка входа Semaphore: развёртывание одной VM."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import (
    InfraManagerError,
    cancel_runtime_activation,
    console,
    require_runtime_activation_idle,
    reserve_runtime_activation,
)
from infra_manager.guest_catalog import guest_identity
from infra_manager.guest_deploy import run_deploy_guest
from infra_manager.guest_operations import extract_survey_vmid
from infra_manager.settings import SETTINGS


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Привести одну VM к состоянию guest.yaml + provision.yaml"
    )
    parser.add_argument("vmid", nargs="?", type=int, help="VMID гостя")
    try:
        cli_args, survey_vmid = extract_survey_vmid(sys.argv[1:])
    except InfraManagerError as exc:
        parser.error(str(exc))
    args = parser.parse_args(cli_args)
    if args.vmid is not None and survey_vmid is not None:
        parser.error("VMID нельзя одновременно передавать позиционно и через GUEST_VMID")
    vmid = survey_vmid if survey_vmid is not None else args.vmid
    if vmid is None:
        parser.error("нужно выбрать гостя или передать VMID")

    activation_reserved = False
    try:
        require_runtime_activation_idle()
        identity = guest_identity(REPO_ROOT, vmid)
        self_update = identity.role == SETTINGS.infra_manager_role
        if self_update:
            reserve_runtime_activation()
            activation_reserved = True

        result = run_deploy_guest(REPO_ROOT, vmid)
        if result != 0 and activation_reserved:
            cancel_runtime_activation()
        return result
    except (InfraManagerError, OSError) as exc:
        if activation_reserved:
            cancel_runtime_activation()
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
