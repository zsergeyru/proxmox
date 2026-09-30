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
from infra_manager.guest_deploy import run_deploy_guest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Привести одну VM к состоянию guest.yaml + provision.yaml"
    )
    parser.add_argument("vmid", type=int, help="VMID гостя")
    parser.add_argument(
        "--bootstrap-scope",
        action="store_true",
        help="Использовать отдельную область первоначального развёртывания",
    )
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument(
        "--infrastructure-only",
        action="store_true",
        help="Только создать/сверить объект Proxmox без Ansible",
    )
    phase.add_argument(
        "--provision-base-only",
        action="store_true",
        help="Применить только базовую часть provision.yaml",
    )
    phase.add_argument(
        "--provision-only",
        action="store_true",
        help="Полностью применить provision.yaml к уже созданному гостю",
    )
    phase.add_argument(
        "--provision-existing-only",
        action="store_true",
        help=(
            "Настроить существующий bootstrap-гость через Ansible "
            "без требования OpenTofu state"
        ),
    )
    args = parser.parse_args()

    activation_reserved = False
    try:
        selected_phase = (
            "infrastructure"
            if args.infrastructure_only
            else "provision-base"
            if args.provision_base_only
            else "provision"
            if args.provision_only
            else "provision-existing"
            if args.provision_existing_only
            else "all"
        )
        require_runtime_activation_idle()
        self_update = (
            args.vmid == 910
            and not args.bootstrap_scope
            and selected_phase == "all"
        )
        if self_update:
            reserve_runtime_activation()
            activation_reserved = True

        result = run_deploy_guest(
            REPO_ROOT,
            args.vmid,
            bootstrap_scope=args.bootstrap_scope,
            phase=selected_phase,
        )
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
