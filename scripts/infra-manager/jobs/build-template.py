#!/usr/bin/env python3
"""Точка входа Semaphore: сборка Packer-шаблона."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console  # noqa: E402
from infra_manager.template_build import run_build_template  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Собрать и проверить Packer-шаблон"
    )
    parser.add_argument("vmid", type=int, help="VMID шаблона")
    args = parser.parse_args()

    try:
        return run_build_template(REPO_ROOT, args.vmid)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
