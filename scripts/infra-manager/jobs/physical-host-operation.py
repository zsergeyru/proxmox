#!/usr/bin/env python3
"""Точка входа физического сервера внутри снимка Git в infra-runtime."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError, console
from infra_manager.physical_host_operations import (
    SUPPORTED_HOST_OPERATIONS,
    run_physical_host_operation,
)


def main() -> int:
    """Выполнить операцию над именованной физической машиной."""

    parser = argparse.ArgumentParser(description="Физический сервер")
    parser.add_argument("operation", choices=sorted(SUPPORTED_HOST_OPERATIONS))
    parser.add_argument("name")
    args = parser.parse_args()
    try:
        return run_physical_host_operation(REPO_ROOT, args.operation, args.name)
    except (InfraManagerError, OSError) as exc:
        console.failure(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
