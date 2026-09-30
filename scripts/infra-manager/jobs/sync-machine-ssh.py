#!/usr/bin/env python3
"""Точка входа Semaphore: синхронизация межмашинного SSH."""

from __future__ import annotations

import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import (
    InfraManagerError,
    console,
    require_runtime_activation_idle,
    run,
)
from infra_manager.machine_ssh import sync_machine_ssh


def main() -> int:
    try:
        require_runtime_activation_idle()
        run(
            [sys.executable, str(REPO_ROOT / "scripts/validate_repo.py")],
        )
        return sync_machine_ssh(REPO_ROOT, verify_connections=True)
    except (InfraManagerError, OSError, ValueError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
