#!/usr/bin/env python3
"""Точка входа Semaphore: первичная инициализация OpenBao infra-manager."""

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
)
from infra_manager.openbao import run_initialize_openbao


def main() -> int:
    try:
        require_runtime_activation_idle()
        return run_initialize_openbao(REPO_ROOT)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
