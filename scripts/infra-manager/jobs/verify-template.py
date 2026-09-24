#!/usr/bin/env python3
"""Точка входа: проверка Packer-шаблона через Full Clone."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console  # noqa: E402
from infra_manager.template import run_verify_template  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверить Packer-шаблон через временный Full Clone"
    )
    parser.add_argument("vmid", type=int, help="VMID шаблона")
    args = parser.parse_args()

    try:
        return run_verify_template(args.vmid)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
