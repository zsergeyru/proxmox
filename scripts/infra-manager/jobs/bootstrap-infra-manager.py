#!/usr/bin/env python3
"""Внутренняя точка входа 990 для bootstrap/recovery infra-manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import (
    InfraManagerError,
    console,
    require_runtime_activation_idle,
)
from infra_manager.guest_deploy import run_bootstrap_infra_manager_step


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Внутренний bootstrap-вызов infra-manager из временного LXC 990"
    )
    parser.add_argument("step", choices=("create", "configure-base", "configure"))
    parser.add_argument("vmid", type=int)
    args = parser.parse_args()

    try:
        require_runtime_activation_idle()
        return run_bootstrap_infra_manager_step(
            REPO_ROOT,
            args.vmid,
            step=args.step,
        )
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
