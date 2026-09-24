#!/usr/bin/env python3
"""Точка входа Semaphore: общий план OpenTofu."""

from __future__ import annotations

import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console  # noqa: E402
from infra_manager.opentofu import run_plan  # noqa: E402


def main() -> int:
    try:
        return run_plan(REPO_ROOT)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
