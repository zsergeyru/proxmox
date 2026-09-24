#!/usr/bin/env python3
"""Модульные и контрактные проверки настройки infra-manager."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import host_setup as host_setup_module
from infra_manager import runtime_setup as runtime_setup_module
from infra_manager.common import InfraManagerError
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

    def stage(self, message: str) -> None:
        self.calls.append(f"reporter.stage:{message}")

    def info(self, message: str) -> None:
        self.calls.append(f"reporter.info:{message}")

    def ok(self, message: str) -> None:
        self.calls.append(f"reporter.ok:{message}")

    def error(self, message: str) -> None:
        self.errors.append(message)


class BehaviorRunner:
    def __init__(self, callback=None) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.callback = callback

    def run(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        command = list(argv)
        self.calls.append((command, dict(kwargs)))
        if self.callback is not None:
            return self.callback(command, kwargs)
        return SimpleNamespace(returncode=0, stdout="")


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


def check_pve_secret_handling() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        staging = root / "staging.env"
        permanent = root / "pve-api.env"
        staging.write_text("PVE_TOKEN=new\n", encoding="utf-8")
        permanent.write_text("PVE_TOKEN=old\n", encoding="utf-8")

        def different_files(argv: list[str], _kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["cmp", "-s"]:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout="")

        runner = BehaviorRunner(different_files)
        reporter = RecordingReporter([])
        context = SetupContext(
            log_file=root / "setup.log",
            staging_secret=str(staging),
            recover=False,
            project_branch="main",
            color=False,
            runner=runner,
            logged_runner=BehaviorRunner(),
        )
        host = HostSetup(context, reporter)

        with (
            patch.object(host_setup_module, "PVE_API_ENV", permanent),
            patch.object(host_setup_module, "install_file") as install_mock,
        ):
            try:
                host.persist_pve_api_secret()
            except InfraManagerError:
                pass
            else:
                fail("Замена PVE API credential без recovery была разрешена")
            install_mock.assert_not_called()

        recovery_context = SetupContext(
            log_file=root / "setup.log",
            staging_secret=str(staging),
            recover=True,
            project_branch="main",
            color=False,
            runner=runner,
            logged_runner=BehaviorRunner(),
        )
        recovery_host = HostSetup(recovery_context, reporter)
        with (
            patch.object(host_setup_module, "PVE_API_ENV", permanent),
            patch.object(host_setup_module, "install_file") as install_mock,
        ):
            recovery_host.persist_pve_api_secret()
            install_mock.assert_called_once_with(
                recovery_context,
                staging,
                permanent,
                "0600",
            )


def check_ansible_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ansible_dir = root / "ansible"
        ansible_dir.mkdir()
        private_key = ansible_dir / "guest_ed25519"
        public_key = ansible_dir / "guest_ed25519.pub"
        private_key.write_text("private-key\n", encoding="utf-8")

        def identity_result(argv: list[str], _kwargs: object) -> SimpleNamespace:
            if argv[:3] == ["ssh-keygen", "-y", "-f"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="ssh-ed25519 AAAATEST original-comment\n",
                )
            return SimpleNamespace(returncode=0, stdout="")

        runner = BehaviorRunner(identity_result)
        logged_runner = BehaviorRunner()
        reporter = RecordingReporter([])
        context = SetupContext(
            log_file=root / "setup.log",
            staging_secret="",
            recover=False,
            project_branch="main",
            color=False,
            runner=runner,
            logged_runner=logged_runner,
        )
        host = HostSetup(context, reporter)

        with (
            patch.object(host_setup_module, "ANSIBLE_DIR", ansible_dir),
            patch.object(host_setup_module, "ANSIBLE_PRIVATE_KEY", private_key),
            patch.object(host_setup_module, "ANSIBLE_PUBLIC_KEY", public_key),
            patch.object(host_setup_module.os, "chown"),
        ):
            host.ensure_ansible_identity()

        expected = "ssh-ed25519 AAAATEST infra-manager ansible guest\n"
        if public_key.read_text(encoding="utf-8") != expected:
            fail("Открытый ключ Ansible не восстановлен из закрытого ключа")
        if private_key.stat().st_mode & 0o777 != 0o600:
            fail("Закрытый ключ Ansible должен иметь режим 0600")
        if public_key.stat().st_mode & 0o777 != 0o644:
            fail("Открытый ключ Ansible должен иметь режим 0644")
        if logged_runner.calls:
            fail("Существующий закрытый ключ Ansible не должен пересоздаваться")


def check_semaphore_storage_repair() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runner = BehaviorRunner()
        logged_runner = BehaviorRunner()
        reporter = RecordingReporter([])
        context = SetupContext(
            log_file=root / "setup.log",
            staging_secret="",
            recover=False,
            project_branch="main",
            color=False,
            runner=runner,
            logged_runner=logged_runner,
        )
        runtime = RuntimeSetup(context, reporter)
        semaphore_dir = root / "semaphore"

        with patch.object(runtime_setup_module, "SEMAPHORE_DIR", semaphore_dir):
            runtime.repair_semaphore_storage()

        commands = [argv for argv, _ in logged_runner.calls]
        if len(commands) != 2:
            fail("Проверка хранилища Semaphore должна выполнять две команды")
        if "0:0" not in commands[0] or "1001:0" not in commands[1]:
            fail("Права хранилища Semaphore проверяются не теми uid/gid")
        expected_volume = f"{semaphore_dir}:/var/lib/semaphore"
        if expected_volume not in commands[0] or expected_volume not in commands[1]:
            fail("Хранилище Semaphore подключено по неверному пути")


def check_wait_semaphore() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def inspect_running(argv: list[str], _kwargs: object) -> SimpleNamespace:
            if argv[:2] == ["docker", "inspect"]:
                return SimpleNamespace(returncode=0, stdout="true\n")
            return SimpleNamespace(returncode=0, stdout="")

        runner = BehaviorRunner(inspect_running)
        logged_runner = BehaviorRunner()
        reporter = RecordingReporter([])
        context = SetupContext(
            log_file=root / "setup.log",
            staging_secret="",
            recover=False,
            project_branch="main",
            color=False,
            runner=runner,
            logged_runner=logged_runner,
        )
        runtime = RuntimeSetup(context, reporter)

        with (
            patch.object(
                RuntimeSetup,
                "semaphore_ready",
                side_effect=[False, True],
            ) as ready_mock,
            patch.object(runtime_setup_module.time, "sleep"),
        ):
            runtime.wait_semaphore()

        if ready_mock.call_count != 2:
            fail("Готовность Semaphore была проверена повторно после успеха")
        if logged_runner.calls:
            fail("Успешное ожидание Semaphore не должно печатать диагностику")
        if not any(
            argv[:2] == ["docker", "inspect"]
            for argv, _ in runner.calls
        ):
            fail("После готовности Semaphore не проверено состояние контейнера")


def check_local_commands_installation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo_root = root / "repo"
        source_package = (
            repo_root / "scripts" / "infra-manager" / "infra_manager"
        )
        source_package.mkdir(parents=True)
        (source_package / "sample.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        commands_dir = repo_root / "scripts" / "infra-manager" / "commands"
        commands_dir.mkdir(parents=True)
        for name in (
            "status.sh",
            "pve-access-check.sh",
            "pve-lifecycle-test.sh",
        ):
            (commands_dir / name).write_text(
                "#!/usr/bin/env bash\n",
                encoding="utf-8",
            )

        python_root = root / "python"
        python_root.mkdir()
        sbin = root / "sbin"
        runner = BehaviorRunner()
        reporter = RecordingReporter([])
        context = SetupContext(
            log_file=root / "setup.log",
            staging_secret="",
            recover=False,
            project_branch="main",
            color=False,
            runner=runner,
            logged_runner=BehaviorRunner(),
        )
        host = HostSetup(context, reporter)

        with (
            patch.object(host_setup_module, "REPO_ROOT", repo_root),
            patch.object(host_setup_module, "PYTHON_INSTALL_ROOT", python_root),
            patch.object(
                host_setup_module,
                "STATUS_COMMAND",
                sbin / "infra-manager-status",
            ),
            patch.object(
                host_setup_module,
                "ACCESS_CHECK_COMMAND",
                sbin / "infra-manager-pve-access-check",
            ),
            patch.object(
                host_setup_module,
                "LIFECYCLE_TEST_COMMAND",
                sbin / "infra-manager-pve-lifecycle-test",
            ),
        ):
            host.install_local_commands()

        installed = python_root / "infra_manager" / "sample.py"
        if not installed.is_file():
            fail("Python-пакет infra_manager не скопирован в постоянный каталог")

        link_commands = [
            argv
            for argv, _ in runner.calls
            if argv and argv[0] == "ln"
        ]
        if len(link_commands) != 3:
            fail("Должны устанавливаться три административные команды")


def main_test() -> None:
    check_setup_contract()
    check_setup_order()
    check_pve_secret_handling()
    check_ansible_identity()
    check_semaphore_storage_repair()
    check_wait_semaphore()
    check_local_commands_installation()
    print("Проверки настройки infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
