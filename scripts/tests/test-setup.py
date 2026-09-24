#!/usr/bin/env python3
"""Модульные и контрактные проверки настройки infra-manager."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.host_setup import (  # noqa: E402
    BASE_PACKAGES,
    PVE_API_ENV,
    HostSetup,
)
from infra_manager.runtime_setup import (  # noqa: E402
    PACKER_VERSION,
    RuntimeSetup,
)
from infra_manager.settings import PATHS, SETTINGS  # noqa: E402
from infra_manager.setup import Setup  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    if "python3" not in BASE_PACKAGES:
        fail("python3 отсутствует в контракте настройки")
    if "python3-yaml" not in BASE_PACKAGES:
        fail("python3-yaml отсутствует в контракте настройки")

    if PATHS.pve_api_env != Path("/etc/infra-manager/secrets/pve-api.env"):
        fail("Канонический путь к учётным данным PVE имеет неожиданное значение")
    if PATHS.ca_bundle != Path("/etc/infra-manager/ca/ca-bundle.crt"):
        fail("Канонический путь к набору сертификатов CA имеет неожиданное значение")
    if PATHS.opentofu_input != Path("/var/lib/infra-manager/opentofu/guests.json"):
        fail("Канонический путь к входным данным OpenTofu имеет неожиданное значение")
    if SETTINGS.default_project_branch != "main":
        fail("Веткой проекта по умолчанию должна быть main")
    if PACKER_VERSION != SETTINGS.packer_version:
        fail("setup не использует версию Packer из общих настроек")
    if PVE_API_ENV != PATHS.pve_api_env:
        fail("setup не использует путь к учётным данным PVE из общих настроек")

    compose = Setup.compose("ps")
    if compose[:2] != ["docker", "compose"] or compose[-1] != "ps":
        fail(f"Неожиданная команда Docker Compose: {compose!r}")
    if not issubclass(Setup, HostSetup) or not issubclass(Setup, RuntimeSetup):
        fail("Setup не объединяет этапы подготовки хоста и среды выполнения")

    print("Проверки настройки infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
