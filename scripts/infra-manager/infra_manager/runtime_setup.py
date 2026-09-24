"""Развёртывание и проверка runtime infra-manager."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import InfraManagerError, command_runner
from .settings import PATHS, SETTINGS

REPO_ROOT = PATHS.repo_root
ASSET_DIR = PATHS.asset_dir
SECRET_DIR = PATHS.secret_dir
SEMAPHORE_DIR = PATHS.semaphore_dir
OPENTOFU_INPUT = PATHS.opentofu_input
COMPOSE_DIR = PATHS.compose_dir
SERVER_ENV = PATHS.server_env
ADMIN_PASSWORD_FILE = PATHS.admin_password_file

SEMAPHORE_VERSION = SETTINGS.semaphore_version
RUNTIME_VERSION = SETTINGS.runtime_version
OPENTOFU_VERSION = SETTINGS.opentofu_version
PACKER_VERSION = SETTINGS.packer_version


class RuntimeSetup:
    """Шаги подготовки Compose, Semaphore и инструментов runtime."""

    def copy_compose_assets(self) -> None:
        assets = (
            ("docker-compose.yml", "docker-compose.yml"),
            ("runtime/Dockerfile", "runtime/Dockerfile"),
            ("runtime/requirements.txt", "runtime/requirements.txt"),
            ("runtime/ssh_config", "runtime/ssh_config"),
        )
        for source_name, target_name in assets:
            source = ASSET_DIR / source_name
            if not source.is_file():
                raise InfraManagerError(
                    f"Не найден обязательный файл: {source}"
                )
            target = COMPOSE_DIR / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            self.install_file(source, target, "0644")

    def seed_semaphore_known_hosts(self) -> None:
        source = Path("/root/.ssh/github_known_hosts")
        target = SEMAPHORE_DIR / "known_hosts"

        if self.nonempty(source):
            self.install_file(source, target, "0644", "1001", "0")
        elif not self.nonempty(target):
            request = urllib.request.Request(
                "https://api.github.com/meta",
                headers={"User-Agent": "infra-manager/1"},
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=20,
                ) as response:
                    data = json.load(response)
            except (
                OSError,
                urllib.error.URLError,
                json.JSONDecodeError,
            ) as exc:
                raise InfraManagerError(
                    f"Не удалось получить SSH host keys GitHub: {exc}"
                ) from exc

            keys = data.get("ssh_keys")
            if not isinstance(keys, list) or not keys:
                raise InfraManagerError(
                    "GitHub API не вернул SSH host keys"
                )
            target.write_text(
                "".join(f"github.com {key}\n" for key in keys),
                encoding="utf-8",
            )
            command_runner.run(["chown", "1001:0", str(target)])
            command_runner.run(["chmod", "0644", str(target)])

        if not self.nonempty(target):
            raise InfraManagerError(
                "Не удалось подготовить known_hosts Runner"
            )

    @staticmethod
    def random_base64(size: int) -> str:
        return command_runner.run(
            ["openssl", "rand", "-base64", str(size)],
            capture=True,
        ).stdout.strip()

    def ensure_semaphore_secrets(self) -> None:
        if not SERVER_ENV.exists():
            admin_password = self.random_base64(24)
            encryption_key = self.random_base64(32)
            timezone = command_runner.run(
                [
                    "timedatectl",
                    "show",
                    "--property=Timezone",
                    "--value",
                ],
                capture=True,
                check=False,
            ).stdout.strip() or "UTC"
            SERVER_ENV.write_text(
                "SEMAPHORE_DB_DIALECT=sqlite\n"
                "SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite\n"
                "SEMAPHORE_ADMIN=admin\n"
                f"SEMAPHORE_ADMIN_PASSWORD={admin_password}\n"
                "SEMAPHORE_ADMIN_NAME=Admin\n"
                "SEMAPHORE_ADMIN_EMAIL=admin@localhost\n"
                f"SEMAPHORE_ACCESS_KEY_ENCRYPTION={encryption_key}\n"
                f"TZ={timezone}\n",
                encoding="utf-8",
            )
            ADMIN_PASSWORD_FILE.write_text(
                f"{admin_password}\n",
                encoding="utf-8",
            )
            SERVER_ENV.chmod(0o600)
            ADMIN_PASSWORD_FILE.chmod(0o600)
            self.ok("Созданы первичные секреты Semaphore")
            return

        if not self.nonempty(ADMIN_PASSWORD_FILE):
            raise InfraManagerError(
                "Есть server env, но отсутствует initial admin password"
            )

        lines = SERVER_ENV.read_text(encoding="utf-8").splitlines()
        SERVER_ENV.write_text(
            "\n".join(
                line
                for line in lines
                if not line.startswith(
                    "SEMAPHORE_USE_REMOTE_RUNNER="
                )
                and not line.startswith(
                    "SEMAPHORE_RUNNER_REGISTRATION_TOKEN="
                )
            )
            + "\n",
            encoding="utf-8",
        )
        SERVER_ENV.chmod(0o600)
        try:
            (SECRET_DIR / "semaphore-runner.env").unlink()
        except FileNotFoundError:
            pass
        self.ok("Используются существующие секреты Semaphore")

    def prepare_opentofu_input(self) -> None:
        self.stage(
            "Подготовка итогового состояния гостей для OpenTofu"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    REPO_ROOT
                    / "scripts/guests/render-opentofu-input.py"
                ),
                "--output",
                str(OPENTOFU_INPUT),
            ]
        )
        command_runner.run(["chown", "1001:0", str(OPENTOFU_INPUT)])
        command_runner.run(["chmod", "0640", str(OPENTOFU_INPUT)])
        self.ok(f"OpenTofu input подготовлен: {OPENTOFU_INPUT}")

    def write_runtime_versions(self) -> None:
        versions = COMPOSE_DIR / ".versions.env"
        versions.write_text(
            f"SEMAPHORE_VERSION={SEMAPHORE_VERSION}\n"
            f"RUNTIME_VERSION={RUNTIME_VERSION}\n"
            f"OPENTOFU_VERSION={OPENTOFU_VERSION}\n"
            f"PACKER_VERSION={PACKER_VERSION}\n",
            encoding="utf-8",
        )
        versions.chmod(0o644)

    @staticmethod
    def compose(*args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(COMPOSE_DIR / ".versions.env"),
            "-f",
            str(COMPOSE_DIR / "docker-compose.yml"),
            *args,
        ]

    def repair_semaphore_storage(self) -> None:
        self.stage("Проверка прав хранилища Semaphore")
        volume = f"{SEMAPHORE_DIR}:/var/lib/semaphore"
        image = f"semaphoreui/semaphore:{SEMAPHORE_VERSION}"

        self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "/bin/sh",
                "-v",
                volume,
                image,
                "-c",
                (
                    "set -eu; "
                    "chown -R 1001:0 /var/lib/semaphore; "
                    "find /var/lib/semaphore -type d "
                    "-exec chmod 0770 {} +; "
                    "find /var/lib/semaphore -type f "
                    "-exec chmod 0660 {} +"
                ),
            ]
        )
        self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "1001:0",
                "--entrypoint",
                "/bin/sh",
                "-v",
                volume,
                image,
                "-c",
                (
                    'set -eu; '
                    'probe="/var/lib/semaphore/.write-test"; '
                    ': >"$probe"; rm -f "$probe"'
                ),
            ]
        )
        self.ok(
            "Хранилище Semaphore доступно для записи из контейнера"
        )

    def deploy_semaphore(self) -> None:
        self.stage("Сборка и запуск Semaphore")
        self.info("Получение базового образа Semaphore")
        self.runner.run(
            [
                "docker",
                "pull",
                f"semaphoreui/semaphore:{SEMAPHORE_VERSION}",
            ]
        )
        self.ok("Базовый образ Semaphore готов")

        command_runner.run(
            self.compose("stop", "runtime"),
            quiet=True,
            check=False,
        )
        self.repair_semaphore_storage()

        self.info(
            "Сборка infra-runtime с Semaphore, OpenTofu, "
            "Packer и Ansible"
        )
        self.runner.run(self.compose("build", "--pull", "runtime"))
        self.ok("Образ infra-runtime собран")

        self.info("Запуск контейнера infra-runtime")
        self.runner.run(
            self.compose("up", "-d", "--remove-orphans")
        )
        self.ok("Контейнер infra-runtime запущен")

    @staticmethod
    def semaphore_ready() -> bool:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:3000/",
                timeout=5,
            ) as response:
                return 200 <= response.status < 400
        except (OSError, urllib.error.URLError):
            return False

    def wait_semaphore(self) -> None:
        self.stage("Проверка Semaphore")
        for _ in range(60):
            if self.semaphore_ready():
                break
            time.sleep(2)

        if not self.semaphore_ready():
            self.runner.run(self.compose("ps"), check=False)
            self.runner.run(
                self.compose("logs", "--tail=100", "runtime"),
                check=False,
            )
            raise InfraManagerError(
                "Semaphore Server не стал доступен"
            )

        time.sleep(5)
        running = command_runner.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                "infra-runtime",
            ],
            capture=True,
            check=False,
        )
        if running.returncode or running.stdout.strip() != "true":
            raise InfraManagerError(
                "Semaphore container не запущен"
            )
        self.ok("Semaphore запущен")

    def verify_semaphore_tools(self) -> None:
        self.stage("Проверка инструментов Semaphore")
        checks = (
            (
                ["docker", "exec", "infra-runtime", "tofu", "version"],
                "OpenTofu",
            ),
            (
                ["docker", "exec", "infra-runtime", "packer", "version"],
                "Packer",
            ),
            (
                [
                    "docker",
                    "exec",
                    "infra-runtime",
                    "ansible",
                    "--version",
                ],
                "Ansible",
            ),
            (
                [
                    "docker",
                    "exec",
                    "infra-runtime",
                    "python3",
                    "-c",
                    "import proxmoxer",
                ],
                "Python-модуль proxmoxer",
            ),
            (
                [
                    "docker",
                    "exec",
                    "infra-runtime",
                    "python3",
                    "-c",
                    "import yaml, jsonschema",
                ],
                "Python-модули проверки проекта",
            ),
        )
        for argv, label in checks:
            try:
                self.runner.run(argv)
            except InfraManagerError as exc:
                raise InfraManagerError(
                    f"{label} отсутствует в Semaphore"
                ) from exc
        self.ok(
            "OpenTofu, Packer, Ansible и proxmoxer доступны"
        )

    def configure_semaphore_project(self) -> None:
        self.stage("Настройка проекта Semaphore")
        from .semaphore import configure_project

        configure_project(self.project_branch)
