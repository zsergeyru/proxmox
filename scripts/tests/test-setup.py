#!/usr/bin/env python3
"""Модульные и контрактные проверки настройки infra-manager."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.host_setup import (
    BASE_PACKAGES,
    PVE_API_ENV,
    HostSetup,
)
from infra_manager.runtime_setup import (
    PACKER_VERSION,
    RuntimeSetup,
)
from infra_manager.settings import PATHS, SETTINGS
from infra_manager.setup import Setup
from infra_manager.setup_context import (
    SetupContext,
    SetupReporter,
)


def fail(message: str) -> None:
    raise SystemExit(message)


class RecordingRunner:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def run(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        self.calls.append(f"runner:{' '.join(argv)}")
        return SimpleNamespace(returncode=0, stdout="")


class RecordingReporter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.errors: list[str] = []

    def initialize(self) -> None:
        self.calls.append("reporter.initialize")

    def error(self, message: str) -> None:
        self.errors.append(message)


class RecordingHost:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __getattr__(self, name: str):
        def record() -> None:
            self.calls.append(f"host.{name}")
        return record


class RecordingRuntime:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __getattr__(self, name: str):
        def record() -> None:
            self.calls.append(f"runtime.{name}")
        return record


def check_setup_contract() -> None:
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

    compose = RuntimeSetup.compose("ps")
    if compose[:2] != ["docker", "compose"] or compose[-1] != "ps":
        fail(f"Неожиданная команда Docker Compose: {compose!r}")

    setup = Setup.from_environment()
    if not isinstance(setup.context, SetupContext):
        fail("Setup не содержит явный SetupContext")
    if not isinstance(setup.reporter, SetupReporter):
        fail("Setup не содержит отдельный SetupReporter")
    if not isinstance(setup.host, HostSetup):
        fail("Setup не содержит HostSetup через композицию")
    if not isinstance(setup.runtime, RuntimeSetup):
        fail("Setup не содержит RuntimeSetup через композицию")
    if setup.host.context is not setup.context or setup.runtime.context is not setup.context:
        fail("HostSetup и RuntimeSetup должны использовать общий SetupContext")
    if setup.host.reporter is not setup.reporter or setup.runtime.reporter is not setup.reporter:
        fail("HostSetup и RuntimeSetup должны использовать общий SetupReporter")


def check_setup_order() -> None:
    calls: list[str] = []
    context = SetupContext(
        log_file=Path("/tmp/infra-manager-test.log"),
        staging_secret="",
        recover=False,
        project_branch="main",
        color=False,
        runner=RecordingRunner(calls),
        logged_runner=RecordingRunner(calls),
    )
    reporter = RecordingReporter(calls)
    host = RecordingHost(calls)
    runtime = RecordingRuntime(calls)
    setup = Setup(
        context=context,
        reporter=reporter,
        host=host,
        runtime=runtime,
    )
    setup.report_result = lambda: calls.append("setup.report_result")

    with patch("infra_manager.setup.os.geteuid", return_value=0):
        if setup.run() != 0:
            fail("Setup.run завершился ошибкой на фиктивных зависимостях")

    expected = [
        "reporter.initialize",
        "host.check_os",
        "host.prepare_directories",
        "host.ensure_base_packages",
        "host.ensure_ansible_identity",
        "host.install_docker",
        "runtime.copy_compose_assets",
        "runtime.seed_semaphore_known_hosts",
        "host.generate_ca_bundle",
        "host.persist_pve_api_secret",
        "runtime.ensure_semaphore_secrets",
        "runtime.prepare_opentofu_input",
        "runtime.write_runtime_versions",
        "runtime.deploy_semaphore",
        "runtime.wait_semaphore",
        "runtime.verify_semaphore_tools",
        "runtime.configure_semaphore_project",
        "host.install_local_commands",
        f"runner:{PATHS.status_command}",
        "setup.report_result",
    ]
    if calls != expected:
        fail(
            "Setup.run нарушил установленный порядок этапов:\n"
            f"ожидалось: {expected!r}\n"
            f"получено: {calls!r}"
        )
    if reporter.errors:
        fail(f"SetupReporter неожиданно получил ошибки: {reporter.errors!r}")


def main_test() -> None:
    check_setup_contract()
    check_setup_order()
    print("Проверки настройки infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
