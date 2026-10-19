"""Системная точка входа задания резервирования внутри управляющего гостя."""

from __future__ import annotations

import json
import sys

from .common import InfraManagerError
from .managed_backup_run import run_backup
from .operator import _pve_node


def main() -> int:
    try:
        result = run_backup(_pve_node())
        print(
            "[ОК] Проверенная зашифрованная копия SSD/NAS: "
            + str(result["created_utc"]),
            flush=True,
        )
        return 0
    except (InfraManagerError, OSError, ValueError) as exc:
        # Сообщения о закрытых данных не передаются во внешний журнал.
        print(f"[ОЖИДАНИЕ] Резервирование не подтверждено: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
