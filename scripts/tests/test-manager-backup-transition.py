#!/usr/bin/env python3
"""Контракт управляемого резервирования и следующего образа runtime."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager import backup_controller
from infra_manager.common import InfraManagerError


def refused(call) -> bool:
    try:
        call()
    except InfraManagerError:
        return True
    return False


def main() -> None:
    guest = ROOT / "infrastructure/guests/910-infra-manager"
    backup = yaml.safe_load((ROOT / "infrastructure/hosts/pve/backup.yaml").read_text())
    timer = (guest / "rootfs/etc/systemd/system/infra-manager-backup.timer").read_text()
    service = (guest / "rootfs/etc/systemd/system/infra-manager-backup.service").read_text()
    tasks = (ROOT / "automation/ansible/roles/infra_manager/tasks/runtime.yml").read_text()
    assert backup["schedule"] == "03,09,15,21:30"
    assert "OnCalendar=*-*-* 03,09,15,21:30:00" in timer
    assert "infra_manager.backup_controller --run" in service
    assert "infra-manager-backup.timer" in tasks
    assert "/mnt/persistent-state/operations" in service
    with patch.object(backup_controller, "POLICY", ROOT / "infrastructure/hosts/pve/backup.yaml"):
        assert refused(backup_controller.policy), "Нельзя резервировать до подтверждения SSD и NAS"
    source = (guest / "rootfs/opt/infra-manager/compose/runtime/Dockerfile.standalone").read_text()
    assert "FROM alpine:" in source
    assert "semaphoreui/semaphore:" not in source
    assert "ansible" in source and "tofu" in source and "packer" in source
    active = (guest / "rootfs/opt/infra-manager/compose/docker-compose.yml").read_text()
    assert "SEMAPHORE_VERSION" in active, "Переход нельзя делать до замены административной панели"
    print("[ОК] Расписание в 910, защита носителей и отдельный образ без Semaphore")


if __name__ == "__main__":
    main()
