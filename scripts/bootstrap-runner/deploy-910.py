#!/usr/bin/env python3
"""Развернуть 910 через общий guest-deploy с отдельным bootstrap-state."""

from __future__ import annotations

import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1] / "infra-manager"
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console
from infra_manager.guest_deploy import run_deploy_guest


def main() -> int:
    try:
        return run_deploy_guest(REPO_ROOT, 910, exclusive=True)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
