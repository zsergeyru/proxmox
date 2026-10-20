#!/usr/bin/env python3
"""Проверка рабочего образа infra-runtime без Semaphore."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import status, common, guest_operations

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
    assert "profiles" not in next_compose["services"]["web"]
    assert next_compose["services"]["web"]["image"] == "infra-web:v1"
    assert next_compose["services"]["web"]["read_only"] is True
    assert next_compose["services"]["openbao"] == current["services"]["openbao"]

    active_yaml = yaml.safe_load(
        (ROOT / "infrastructure/guests/910-infra-manager/infra-manager-status-standalone.yaml").read_text()
    )
    checks = {entry["type"] for entry in active_yaml["checks"]}
    assert "semaphore" not in checks
    assert {"runtime", "infra_web", "openbao", "runtime_tools", "pve_access"} <= checks

    with tempfile.TemporaryDirectory() as temp:
        compose = Path(temp)
        versions = compose / ".versions.env"
        with patch.object(status, "PATHS", SimpleNamespace(compose_dir=compose)):
            assert status._standalone_runtime() is False
            versions.write_text("RUNTIME_MODE=semaphore\n", encoding="utf-8")
            assert status._standalone_runtime() is False
            versions.write_text("RUNTIME_MODE=standalone\n", encoding="utf-8")
            assert status._standalone_runtime() is True

    # Новый marker общий для LXC и runtime; SQLite Semaphore не нужен.
    with patch.dict("os.environ", {"INFRA_RUNTIME_MODE": "standalone"}):
        with tempfile.TemporaryDirectory() as directory:
            test_root = Path(directory)
            with (
                patch.object(common, "HOST_OPERATIONS_STATE_DIR", test_root / "host"),
                patch.object(common, "RUNTIME_OPERATIONS_STATE_DIR", test_root / "runtime"),
            ):
                assert common._default_runtime_activation_marker() == (
                    test_root / "runtime/runtime-activation-pending"
                )
                (test_root / "host").mkdir()
                assert common._default_runtime_activation_marker() == (
                    test_root / "host/runtime-activation-pending"
                )

    # Sync/repair пользуются существующим исполнителем, а не API Semaphore.
    with (
        patch.dict("os.environ", {"INFRA_RUNTIME_MODE": "standalone"}),
        patch.object(guest_operations, "console") as console,
        patch.object(guest_operations, "install_openbao_host_support") as install,
        patch.object(guest_operations, "repair_openbao_on_host") as repair,
    ):
        guest_operations._infra_manager_sync(ROOT, object())
        guest_operations._infra_manager_repair("pve", ROOT, object())
        install.assert_called_once()
        repair.assert_called_once_with("pve")
        assert console.ok.call_count == 2

    activate = (ROOT / "scripts/infra-manager/commands/activate-runtime.sh").read_text()
    assert "RUNTIME_MODE=standalone" in activate
    assert "python3 -m infra_manager.operation_activation verifying" in activate
    assert 'TRANSITION_MARKER="/mnt/persistent-state/locks/runtime-activation-pending"' in activate
    assert 'ACTIVATION_MARKER="$TRANSITION_MARKER"' in activate
    assert 'MARKER_VERIFIED=1' in activate
    assert "/var/lib/semaphore" not in activate
    print("[ОК] Рабочий infra-runtime не зависит от Semaphore")


if __name__ == "__main__":
    main()
