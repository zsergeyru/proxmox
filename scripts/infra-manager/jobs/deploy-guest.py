#!/usr/bin/env python3
"""Точка входа Semaphore: развёртывание одной VM."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console
from infra_manager.guest_deploy import run_deploy_guest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Привести одну VM к состоянию guest.yaml + provision.yaml"
    )
    parser.add_argument("vmid", type=int, help="VMID гостя")
    args = parser.parse_args()

    try:
        return run_deploy_guest(REPO_ROOT, args.vmid)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
