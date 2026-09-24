"""Оркестрация настройки LXC 910 infra-manager.

Public Bootstrap вызывает setup.sh, а тот передаёт управление этому модулю.
Подготовка хоста и runtime разделена между специализированными исполнителями.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .common import CommandRunner, InfraManagerError, command_runner
from .host_setup import PVE_API_ENV, HostSetup
from .runtime_setup import RuntimeSetup
from .settings import PATHS, SETTINGS

STATUS_COMMAND = PATHS.status_command
ADMIN_PASSWORD_FILE = PATHS.admin_password_file
ADMIN_PASSWORD_SHOWN_FILE = PATHS.admin_password_shown_file


@dataclass
class Setup(HostSetup, RuntimeSetup):
    """Параметры, общие операции и порядок полного setup."""

    log_file: Path
    staging_secret: str
    recover: bool
    project_branch: str
    color: bool

    @classmethod
    def from_environment(cls) -> Setup:
        return cls(
            log_file=Path(
                os.environ.get(
                    "INFRA_MANAGER_LOG_FILE",
                    "/var/log/infra-manager/bootstrap.log",
                )
            ),
            staging_secret=os.environ.get("PVE_API_SECRET_FILE", ""),
            recover=os.environ.get("INFRA_MANAGER_RECOVER", "0") == "1",
            project_branch=SETTINGS.project_branch(),
            color=(
                os.environ.get("INFRA_MANAGER_COLOR", "0") == "1"
                and not os.environ.get("NO_COLOR")
            ),
        )

    def c(self, code: str) -> str:
        return code if self.color else ""

    @property
    def runner(self) -> CommandRunner:
        """Исполнитель команд setup с записью вывода в технический лог."""
        return CommandRunner(default_log_file=self.log_file)

    def init_log(self) -> None:
        command_runner.run(
            [
                "install",
                "-d",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                str(self.log_file.parent),
            ]
        )
        self.log_file.touch(exist_ok=True)
        self.log_file.chmod(0o640)
        with self.log_file.open("a", encoding="utf-8") as stream:
            stamp = datetime.now(timezone.utc).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            stream.write(f"\n===== setup infra-manager {stamp} =====\n")

    def write_log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with self.log_file.open("a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {message}\n")

    def stage(self, message: str) -> None:
        self.write_log(f"ЭТАП: {message}")
        print(
            f"\n{self.c(chr(27)+'[1m')}{self.c(chr(27)+'[34m')}"
            f"==> {message}{self.c(chr(27)+'[0m')}"
        )

    def ok(self, message: str) -> None:
        self.write_log(f"ОК: {message}")
        print(
            f"{self.c(chr(27)+'[1m')}{self.c(chr(27)+'[32m')}"
            f"[ОК]{self.c(chr(27)+'[0m')} {message}"
        )

    def info(self, message: str) -> None:
        self.write_log(f"ИНФО: {message}")
        print(
            f"{self.c(chr(27)+'[1m')}{self.c(chr(27)+'[36m')}"
            f"[ИНФО]{self.c(chr(27)+'[0m')} {message}"
        )

    def error(self, message: str) -> None:
        try:
            self.write_log(f"ОШИБКА: {message}")
        except OSError:
            pass
        print(
            f"\n{self.c(chr(27)+'[1m')}{self.c(chr(27)+'[31m')}"
            f"ОШИБКА:{self.c(chr(27)+'[0m')} {message}",
            file=sys.stderr,
        )
        print(f"Полный технический лог: {self.log_file}", file=sys.stderr)

    @staticmethod
    def nonempty(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    @staticmethod
    def install_file(
        source: Path,
        target: Path,
        mode: str,
        owner: str = "root",
        group: str = "root",
    ) -> None:
        command_runner.run(
            [
                "install",
                "-o",
                owner,
                "-g",
                group,
                "-m",
                mode,
                str(source),
                str(target),
            ]
        )

    def report_result(self) -> None:
        address = command_runner.run(
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
            self.init_log()

            self.check_os()
            self.prepare_directories()
            self.ensure_base_packages()
            self.ensure_ansible_identity()
            self.install_docker()
            self.copy_compose_assets()
            self.seed_semaphore_known_hosts()
            self.generate_ca_bundle()
            self.persist_pve_api_secret()
            self.ensure_semaphore_secrets()
            self.prepare_opentofu_input()
            self.write_runtime_versions()
            self.deploy_semaphore()
            self.wait_semaphore()
            self.verify_semaphore_tools()
            self.configure_semaphore_project()
            self.install_local_commands()

            command_runner.run([str(STATUS_COMMAND)])
            self.report_result()
            return 0
        except (InfraManagerError, OSError) as exc:
            self.error(str(exc))
            return 1


def run_setup() -> int:
    return Setup.from_environment().run()
