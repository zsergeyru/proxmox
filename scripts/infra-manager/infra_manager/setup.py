"""Оркестрация настройки LXC 910 infra-manager.

Public Bootstrap вызывает setup.sh, а тот передаёт управление этому модулю.
Подготовка хоста и runtime выполняется отдельными объектами через явный контекст.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .common import CommandRunner, InfraManagerError
from .host_setup import PVE_API_ENV, HostSetup
from .runtime_setup import RuntimeSetup
from .settings import PATHS, SETTINGS
from .setup_context import SetupContext, SetupReporter

STATUS_COMMAND = PATHS.status_command
ADMIN_PASSWORD_FILE = PATHS.admin_password_file
ADMIN_PASSWORD_SHOWN_FILE = PATHS.admin_password_shown_file


@dataclass
class Setup:
    """Оркестратор полного процесса настройки infra-manager."""

    context: SetupContext
    reporter: SetupReporter
    host: HostSetup
    runtime: RuntimeSetup

    @classmethod
    def from_environment(cls) -> Setup:
        log_file = Path(
            os.environ.get(
                "INFRA_MANAGER_LOG_FILE",
                "/var/log/infra-manager/bootstrap.log",
            )
        )
        context = SetupContext(
            log_file=log_file,
            staging_secret=os.environ.get("PVE_API_SECRET_FILE", ""),
            recover=os.environ.get("INFRA_MANAGER_RECOVER", "0") == "1",
            project_branch=SETTINGS.project_branch(),
            color=(
                os.environ.get("INFRA_MANAGER_COLOR", "0") == "1"
                and not os.environ.get("NO_COLOR")
            ),
            runner=CommandRunner(),
            logged_runner=CommandRunner(default_log_file=log_file),
        )
        reporter = SetupReporter(context)
        return cls(
            context=context,
            reporter=reporter,
            host=HostSetup(context, reporter),
            runtime=RuntimeSetup(context, reporter),
        )

    def report_result(self) -> None:
        address = self.context.runner.run(
            ["hostname", "-I"],
            capture=True,
            check=False,
        ).stdout.split()

        print("\n910 infra-manager подготовлен.")
        print(
            f"Semaphore: http://{address[0]}:3000/"
            if address
            else "Semaphore: порт 3000 LXC 910"
        )
        print("Пользователь: admin")

        if not ADMIN_PASSWORD_SHOWN_FILE.exists():
            password = ADMIN_PASSWORD_FILE.read_text(
                encoding="utf-8"
            ).strip()
            print(f"Пароль: {password}")
            print(
                "Пароль показан один раз и сохранён: "
                f"{ADMIN_PASSWORD_FILE}"
            )
            ADMIN_PASSWORD_SHOWN_FILE.touch()
            ADMIN_PASSWORD_SHOWN_FILE.chmod(0o600)
        else:
            print(f"Пароль сохранён: {ADMIN_PASSWORD_FILE}")

        print(f"PVE API credential хранится: {PVE_API_ENV}")

    def run(self) -> int:
        if os.geteuid() != 0:
            print(
                "ОШИБКА: setup.sh должен выполняться от root",
                file=sys.stderr,
            )
            return 1

        try:
            self.reporter.initialize()

            self.host.check_os()
            self.host.prepare_directories()
            self.host.ensure_base_packages()
            self.host.ensure_ansible_identity()
            self.host.install_docker()
            self.runtime.copy_compose_assets()
            self.runtime.seed_semaphore_known_hosts()
            self.host.generate_ca_bundle()
            self.host.persist_pve_api_secret()
            self.runtime.ensure_semaphore_secrets()
            self.runtime.prepare_opentofu_input()
            self.runtime.write_runtime_versions()
            self.runtime.deploy_semaphore()
            self.runtime.wait_semaphore()
            self.runtime.verify_semaphore_tools()
            self.runtime.configure_semaphore_project()
            self.host.install_local_commands()

            self.context.runner.run([str(STATUS_COMMAND)])
            self.report_result()
            return 0
        except (InfraManagerError, OSError) as exc:
            self.reporter.error(str(exc))
            return 1


def run_setup() -> int:
    return Setup.from_environment().run()
