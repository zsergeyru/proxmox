#!/usr/bin/env python3
"""Точка входа Semaphore для стандартных операций над гостем."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console
from infra_manager.guest_operations import (
    GUEST_OPERATIONS,
    extract_survey_vmid,
    run_guest_operation,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Выполнить стандартную операцию над выбранным гостем"
    )
    parser.add_argument(
        "operation",
        choices=GUEST_OPERATIONS,
        help="Операция: deploy/status/repair/test/sync",
    )
    parser.add_argument("vmid", nargs="?", type=int, help="VMID гостя")

    try:
        cli_args, survey_vmid = extract_survey_vmid(sys.argv[1:])
    except InfraManagerError as exc:
        parser.error(str(exc))

    args = parser.parse_args(cli_args)
    if args.vmid is not None and survey_vmid is not None:
        parser.error(
            "VMID нельзя одновременно передавать позиционно и через GUEST_VMID"
        )
    vmid = survey_vmid if survey_vmid is not None else args.vmid
    if vmid is None:
        parser.error("нужно выбрать гостя или передать VMID")

    try:
        return run_guest_operation(REPO_ROOT, args.operation, vmid)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
