#!/usr/bin/env python3
"""Контракт нового образа infra-runtime без включения опасного переключения."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import status

base = (
    ROOT / "infrastructure/guests/910-infra-manager/rootfs"
    / "opt/infra-manager/compose"
)


def main() -> None:
    image = (base / "runtime-next/Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12" in image
    assert "ansible-core==" in image
    assert 'CMD ["sleep", "infinity"]' in image
    assert "FROM semaphoreui/" not in image

    current = yaml.safe_load((base / "docker-compose.yml").read_text())
    next_compose = yaml.safe_load((base / "docker-compose-standalone.yml").read_text())
    new = next_compose["services"]["runtime"]
    assert set(current["services"]) == set(next_compose["services"])
    assert new["build"]["dockerfile"] == "runtime-next/Dockerfile"
    assert "env_file" not in new
    assert not any("semaphore" in volume.lower() for volume in new["volumes"])
    assert next_compose["services"]["web"]["profiles"] == ["preview"]
    assert next_compose["services"]["web"]["read_only"] is True
    assert next_compose["services"]["openbao"] == current["services"]["openbao"]

    active_yaml = yaml.safe_load(
        (ROOT / "infrastructure/guests/910-infra-manager/infra-manager-status-standalone.yaml").read_text()
    )
    checks = {entry["type"] for entry in active_yaml["checks"]}
    assert "semaphore" not in checks
    assert {"runtime", "openbao", "runtime_tools", "pve_access"} <= checks

    with tempfile.TemporaryDirectory() as temp:
        compose = Path(temp)
        versions = compose / ".versions.env"
        with patch.object(status, "PATHS", SimpleNamespace(compose_dir=compose)):
            assert status._standalone_runtime() is False
            versions.write_text("RUNTIME_MODE=semaphore\n", encoding="utf-8")
            assert status._standalone_runtime() is False
            versions.write_text("RUNTIME_MODE=standalone\n", encoding="utf-8")
            assert status._standalone_runtime() is True

    activate = (ROOT / "scripts/infra-manager/commands/activate-runtime.sh").read_text()
    assert "RUNTIME_MODE=standalone" in activate
    assert "python3 -m infra_manager.operation_activation verifying" in activate
    print("[ОК] Следующий infra-runtime не зависит от Semaphore; активный runtime не заменён")


if __name__ == "__main__":
    main()
