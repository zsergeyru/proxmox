#!/usr/bin/env python3
"""Операторская команда резервирования прямо на физическом PVE."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/sbin")
from infra_manager_backup import dispatch_backup


def main() -> int:
    if os.geteuid() != 0:
        print("ОШИБКА: резервирование запускается только root на PVE", file=sys.stderr)
        return 1
    try:
        return dispatch_backup(sys.argv[1:], RuntimeError)
    except RuntimeError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
