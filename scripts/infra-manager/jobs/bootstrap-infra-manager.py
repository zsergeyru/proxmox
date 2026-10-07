#!/usr/bin/env python3
"""Внутренняя точка входа 990 для bootstrap/recovery infra-manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console, require_runtime_activation_idle
from infra_manager.guest_catalog import guest_identity
from infra_manager.guest_deploy import run_deploy_guest
from infra_manager.settings import SETTINGS


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Внутренний bootstrap-вызов infra-manager из временного LXC 990"
    )
    parser.add_argument("phase", choices=("infrastructure", "base", "provision", "existing"))
    parser.add_argument("vmid", type=int)
    args = parser.parse_args()

    try:
        require_runtime_activation_idle()
        identity = guest_identity(REPO_ROOT, args.vmid)
        if identity.role != SETTINGS.infra_manager_role:
            raise InfraManagerError(
                f"Гость {args.vmid} не имеет роль {SETTINGS.infra_manager_role!r}"
            )

        phase = {
            "infrastructure": "infrastructure",
            "base": "provision-base",
            "provision": "provision",
            "existing": "provision-existing",
        }[args.phase]
        return run_deploy_guest(
            REPO_ROOT,
            args.vmid,
            bootstrap_scope=True,
            phase=phase,
        )
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
