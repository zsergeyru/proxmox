#!/usr/bin/env python3
"""Изменение постоянного уровня вывода заданий Semaphore."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import (
    LOG_LEVEL_ENV,
    LOG_LEVELS,
    InfraManagerError,
    console,
)
from infra_manager.semaphore import set_log_level_from_task


def _extract_level(argv: list[str]) -> tuple[list[str], str | None]:
    remaining: list[str] = []
    values: list[str] = []
    prefix = f"{LOG_LEVEL_ENV}="

    for item in argv:
        if item.startswith(prefix):
            values.append(item.split("=", 1)[1])
        else:
            remaining.append(item)

    if len(values) > 1:
        raise InfraManagerError(
            f"{LOG_LEVEL_ENV} передан более одного раза"
        )
    return remaining, values[0] if values else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Сохранить уровень вывода инфраструктурных заданий"
    )

    try:
        cli_args, selected = _extract_level(sys.argv[1:])
    except InfraManagerError as exc:
        parser.error(str(exc))

    parser.parse_args(cli_args)
    if selected is None:
        parser.error("нужно выбрать режим вывода")
    if selected not in LOG_LEVELS:
        allowed = ", ".join(sorted(LOG_LEVELS))
        parser.error(
            f"{LOG_LEVEL_ENV} должен быть одним из значений: {allowed}"
        )

    try:
        return set_log_level_from_task(selected)
    except (InfraManagerError, OSError) as exc:
        console.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
