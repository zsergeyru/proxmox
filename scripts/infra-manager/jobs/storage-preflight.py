#!/usr/bin/env python3
"""Проверить SSD и NFS перед deploy 910 кодом только что полученного Git.

Этот процесс запускается в 910 после git checkout под общей блокировкой
репозитория. Старый оператор остаётся оболочкой; импорт модулей происходит
заново из обновлённой рабочей копии, до её установки через Ansible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.common import InfraManagerError, console
from infra_manager.pve_backup_nas import deploy_backup_nas_preflight
from infra_manager.pve_backup_ssd import deploy_backup_ssd_preflight


def main() -> int:
    """Сверить носители через ограниченные функции проекта, без общего shell."""
    if os.geteuid() != 0:
        raise InfraManagerError("Проверка SSD/NAS требует root внутри 910")
    deploy_backup_ssd_preflight()
    deploy_backup_nas_preflight()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InfraManagerError, OSError, ValueError) as exc:
        console.failure(exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        console.warning("Проверка дисков прервана; deploy не продолжен")
        raise SystemExit(130) from None
