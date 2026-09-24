"""Настройка LXC 910 infra-manager на Python.

Внешний контракт остаётся прежним: Public Bootstrap вызывает setup.sh, а тот
передаёт управление этому модулю. Настройка Semaphore выполняется напрямую
через Python-модуль; status и PVE access сохраняют совместимые shell-команды.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .common import InfraManagerError
from .settings import PATHS, SETTINGS

REPO_ROOT = PATHS.repo_root
ASSET_DIR = PATHS.asset_dir
CONFIG_DIR = PATHS.config_dir
SECRET_DIR = PATHS.secret_dir
CA_DIR = PATHS.ca_dir
DATA_DIR = PATHS.data_dir
SEMAPHORE_DIR = PATHS.semaphore_dir
OPENTOFU_DIR = PATHS.opentofu_dir
STATE_DIR = PATHS.opentofu_state_dir
OPENTOFU_INPUT = PATHS.opentofu_input
COMPOSE_DIR = PATHS.compose_dir
PYTHON_INSTALL_ROOT = PATHS.python_install_root
STATUS_COMMAND = PATHS.status_command
ACCESS_CHECK_COMMAND = PATHS.access_check_command
LIFECYCLE_TEST_COMMAND = PATHS.lifecycle_test_command
SERVER_ENV = PATHS.server_env
PVE_API_ENV = PATHS.pve_api_env
ADMIN_PASSWORD_FILE = PATHS.admin_password_file
ADMIN_PASSWORD_SHOWN_FILE = PATHS.admin_password_shown_file
CA_BUNDLE = PATHS.ca_bundle
ANSIBLE_DIR = PATHS.ansible_dir
ANSIBLE_PRIVATE_KEY = PATHS.ansible_private_key
ANSIBLE_PUBLIC_KEY = PATHS.ansible_public_key

SEMAPHORE_VERSION = SETTINGS.semaphore_version
RUNTIME_VERSION = SETTINGS.runtime_version
OPENTOFU_VERSION = SETTINGS.opentofu_version
PACKER_VERSION = SETTINGS.packer_version

BASE_PACKAGES = (
    "ca-certificates",
    "curl",
    "git",
    "gnupg",
    "jq",
    "openssh-client",
    "openssl",
    "python3",
    "python3-yaml",
)
DOCKER_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
DOCKER_CONFLICT_PACKAGES = (
    "docker.io",
    "docker-compose",
    "docker-doc",
    "podman-docker",
    "containerd",
    "runc",
)


@dataclass
class Setup:
    log_file: Path
    staging_secret: str
    recover: bool
    project_branch: str
    color: bool

    @classmethod
    def from_environment(cls) -> "Setup":
        return cls(
            log_file=Path(
                os.environ.get(
                    "INFRA_MANAGER_LOG_FILE",
                    "/var/log/infra-manager/bootstrap.log",
                )
            ),
            staging_secret=os.environ.get("PVE_API_SECRET_FILE", ""),
            recover=os.environ.get("INFRA_MANAGER_RECOVER", "0") == "1",
            project_branch=os.environ.get("INFRA_PROJECT_BRANCH", SETTINGS.default_project_branch),
            color=(
                os.environ.get("INFRA_MANAGER_COLOR", "0") == "1"
                and not os.environ.get("NO_COLOR")
            ),
        )

    def c(self, code: str) -> str:
        return code if self.color else ""

    def init_log(self) -> None:
        self.run_raw(
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
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stream.write(f"\n===== setup infra-manager {stamp} =====\n")

    def write_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    def run_raw(
        argv: list[str],
        *,
        capture: bool = False,
        quiet: bool = False,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=check,
            text=True,
            stdout=subprocess.PIPE
            if capture
            else (subprocess.DEVNULL if quiet else None),
            stderr=subprocess.DEVNULL if quiet else None,
            env=env,
        )

    def run_logged(
        self,
        argv: list[str],
        *,
        env_updates: dict[str, str] | None = None,
    ) -> None:
        self.write_log(f"КОМАНДА: {shlex.join(argv)}")
        env = os.environ.copy()
        if env_updates:
            env.update(env_updates)

        with self.log_file.open("a", encoding="utf-8") as stream:
            result = subprocess.run(
                argv,
                check=False,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )

        if result.returncode != 0:
            tail = self.log_file.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()[-25:]
            detail = "\n".join(tail)
            raise InfraManagerError(
                f"команда завершилась с кодом {result.returncode}: "
                f"{shlex.join(argv)}"
                + (f"\nПоследние строки лога:\n{detail}" if detail else "")
            )

    def run_visible(
        self,
        argv: list[str],
        *,
        env_updates: dict[str, str] | None = None,
    ) -> None:
        self.write_log(f"КОМАНДА: {shlex.join(argv)}")
        env = os.environ.copy()
        if env_updates:
            env.update(env_updates)
        result = subprocess.run(argv, check=False, env=env)
        if result.returncode:
            raise InfraManagerError(
                f"команда завершилась с кодом {result.returncode}: "
                f"{shlex.join(argv)}"
            )

    @staticmethod
    def installed(package: str) -> bool:
        return (
            subprocess.run(
                ["dpkg", "-s", package],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )

    @staticmethod
    def nonempty(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    @staticmethod
    def os_release() -> dict[str, str]:
        result: dict[str, str] = {}
        for line in Path("/etc/os-release").read_text(
            encoding="utf-8"
        ).splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key] = value.strip().strip('"').strip("'")
        return result

    @staticmethod
    def install_file(
        source: Path,
        target: Path,
        mode: str,
        owner: str = "root",
        group: str = "root",
    ) -> None:
        Setup.run_raw(
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

    def check_os(self) -> None:
        release = self.os_release()
        if release.get("ID") != "debian":
            raise InfraManagerError("Ожидается Debian")
        if release.get("VERSION_ID") != "13":
            raise InfraManagerError(
                f"Ожидается Debian 13, обнаружено "
                f"{release.get('VERSION_ID', '?')}"
            )
        architecture = self.run_raw(
            ["dpkg", "--print-architecture"],
            capture=True,
        ).stdout.strip()
        if architecture != "amd64":
            raise InfraManagerError(
                "Первая версия infra-manager рассчитана на amd64"
            )

    def prepare_directories(self) -> None:
        for path in (CONFIG_DIR, CA_DIR, DATA_DIR, COMPOSE_DIR):
            self.run_raw(
                [
                    "install", "-d", "-o", "root", "-g", "root",
                    "-m", "0755", str(path),
                ]
            )
        self.run_raw(
            [
                "install", "-d", "-o", "root", "-g", "root",
                "-m", "0700", str(SECRET_DIR),
            ]
        )
        self.run_raw(
            [
                "install", "-d", "-o", "1001", "-g", "0",
                "-m", "0770", str(SEMAPHORE_DIR),
            ]
        )
        for path in (OPENTOFU_DIR, STATE_DIR):
            self.run_raw(
                [
                    "install", "-d", "-o", "1001", "-g", "0",
                    "-m", "0750", str(path),
                ]
            )
        self.run_raw(["chown", "-R", "1001:0", str(SEMAPHORE_DIR)])
        self.run_raw(["chmod", "0770", str(SEMAPHORE_DIR)])

    def ensure_ansible_identity(self) -> None:
        """Создать или проверить постоянную SSH-идентичность Ansible."""
        self.stage("Проверка SSH-идентичности Ansible")
        self.run_raw(
            [
                "install", "-d", "-o", "1001", "-g", "0",
                "-m", "0700", str(ANSIBLE_DIR),
            ]
        )

        if ANSIBLE_PUBLIC_KEY.exists() and not ANSIBLE_PRIVATE_KEY.exists():
            raise InfraManagerError(
                "Есть открытый ключ Ansible без закрытого ключа"
            )

        if not ANSIBLE_PRIVATE_KEY.exists():
            self.run_logged(
                [
                    "ssh-keygen",
                    "-q",
                    "-t", "ed25519",
                    "-N", "",
                    "-C", "infra-manager ansible guest",
                    "-f", str(ANSIBLE_PRIVATE_KEY),
                ]
            )

        derived = self.run_raw(
            ["ssh-keygen", "-y", "-f", str(ANSIBLE_PRIVATE_KEY)],
            capture=True,
        ).stdout.strip()
        if not derived:
            raise InfraManagerError(
                "Не удалось получить открытый ключ из Ansible private key"
            )

        parts = derived.split()
        if len(parts) < 2:
            raise InfraManagerError(
                "Открытый ключ Ansible имеет некорректный формат"
            )
        normalized_public_key = (
            f"{parts[0]} {parts[1]} infra-manager ansible guest\n"
        )
        if (
            not ANSIBLE_PUBLIC_KEY.is_file()
            or ANSIBLE_PUBLIC_KEY.read_text(encoding="utf-8")
            != normalized_public_key
        ):
            ANSIBLE_PUBLIC_KEY.write_text(
                normalized_public_key,
                encoding="utf-8",
            )

        os.chown(ANSIBLE_PRIVATE_KEY, 1001, 0)
        os.chown(ANSIBLE_PUBLIC_KEY, 1001, 0)
        ANSIBLE_PRIVATE_KEY.chmod(0o600)
        ANSIBLE_PUBLIC_KEY.chmod(0o644)
        self.ok("SSH-идентичность Ansible готова")

    def ensure_base_packages(self) -> None:
        missing = [
            package for package in BASE_PACKAGES if not self.installed(package)
        ]
        if not missing:
            self.ok("Базовые пакеты infra-manager уже установлены")
            return

        self.stage("Установка базовых пакетов infra-manager")
        self.run_logged(["apt-get", "update"])
        self.run_logged(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *missing,
            ],
            env_updates={"DEBIAN_FRONTEND": "noninteractive"},
        )
        self.ok("Базовые пакеты infra-manager установлены")

    def install_docker(self) -> None:
        docker_ready = (
            subprocess.run(
                ["docker", "compose", "version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
            if shutil.which("docker")
            else False
        )
        if docker_ready:
            self.run_logged(["systemctl", "enable", "--now", "docker"])
            self.ok("Docker Engine и Compose уже доступны")
            return

        for package in DOCKER_CONFLICT_PACKAGES:
            if self.installed(package):
                raise InfraManagerError(
                    f"Обнаружен конфликтующий пакет {package}. "
                    "Удалите его осознанно перед установкой Docker CE."
                )

        self.stage("Установка Docker Engine из официального репозитория")
        self.run_logged(["apt-get", "update"])
        self.run_raw(["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
        self.run_logged(
            [
                "curl",
                "-fsSL",
                "https://download.docker.com/linux/debian/gpg",
                "-o",
                "/etc/apt/keyrings/docker.asc",
            ]
        )
        self.run_raw(["chmod", "a+r", "/etc/apt/keyrings/docker.asc"])

        release = self.os_release()
        architecture = self.run_raw(
            ["dpkg", "--print-architecture"],
            capture=True,
        ).stdout.strip()
        codename = release.get("VERSION_CODENAME") or "trixie"
        docker_sources = Path("/etc/apt/sources.list.d/docker.sources")
        docker_sources.write_text(
            "Types: deb\n"
            "URIs: https://download.docker.com/linux/debian\n"
            f"Suites: {codename}\n"
            "Components: stable\n"
            f"Architectures: {architecture}\n"
            "Signed-By: /etc/apt/keyrings/docker.asc\n",
            encoding="utf-8",
        )
        docker_sources.chmod(0o644)

        self.run_logged(["apt-get", "update"])
        self.run_logged(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *DOCKER_PACKAGES,
            ],
            env_updates={"DEBIAN_FRONTEND": "noninteractive"},
        )
        self.run_logged(["systemctl", "enable", "--now", "docker"])
        self.run_logged(["docker", "version"])
        self.run_logged(["docker", "compose", "version"])
        self.ok("Docker Engine и Compose установлены")

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
            self.run_raw(["chown", "1001:0", str(target)])
            self.run_raw(["chmod", "0644", str(target)])

        if not self.nonempty(target):
            raise InfraManagerError(
                "Не удалось подготовить known_hosts Runner"
            )

    def generate_ca_bundle(self) -> None:
        system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
        pve_ca = Path(
            "/usr/local/share/ca-certificates/pve-root-ca.crt"
        )
        if not self.nonempty(system_ca):
            raise InfraManagerError("Не найден системный CA bundle")
        if not self.nonempty(pve_ca):
            raise InfraManagerError(
                "Не найден PVE CA, который должен передать public bootstrap"
            )
        CA_BUNDLE.write_bytes(
            system_ca.read_bytes() + pve_ca.read_bytes()
        )
        CA_BUNDLE.chmod(0o644)

    def persist_pve_api_secret(self) -> None:
        staging = Path(self.staging_secret) if self.staging_secret else None

        if PVE_API_ENV.is_file():
            if staging is not None and self.nonempty(staging):
                same = (
                    subprocess.run(
                        ["cmp", "-s", str(staging), str(PVE_API_ENV)],
                        check=False,
                    ).returncode
                    == 0
                )
                if same:
                    self.ok(
                        "Постоянный PVE API credential уже актуален"
                    )
                    return
                if not self.recover:
                    raise InfraManagerError(
                        "Постоянный PVE API credential отличается "
                        "от переданного. Для замены требуется recovery."
                    )
                self.install_file(staging, PVE_API_ENV, "0600")
                self.ok(
                    "PVE API credential заменён в режиме recovery"
                )
                return

            self.ok(
                "Используется существующий постоянный PVE API credential"
            )
            return

        if staging is None:
            raise InfraManagerError("Не задан PVE_API_SECRET_FILE")
        if not self.nonempty(staging):
            raise InfraManagerError(
                f"PVE API staging secret отсутствует: {staging}"
            )
        self.install_file(staging, PVE_API_ENV, "0600")
        self.ok(
            "PVE API credential сохранён в защищённом "
            "постоянном хранилище"
        )

    def random_base64(self, size: int) -> str:
        return self.run_raw(
            ["openssl", "rand", "-base64", str(size)],
            capture=True,
        ).stdout.strip()

    def ensure_semaphore_secrets(self) -> None:
        if not SERVER_ENV.exists():
            admin_password = self.random_base64(24)
            encryption_key = self.random_base64(32)
            timezone = self.run_raw(
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
        self.run_logged(
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
        self.run_raw(["chown", "1001:0", str(OPENTOFU_INPUT)])
        self.run_raw(["chmod", "0640", str(OPENTOFU_INPUT)])
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

        self.run_logged(
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
                "set -eu; "
                "chown -R 1001:0 /var/lib/semaphore; "
                "find /var/lib/semaphore -type d "
                "-exec chmod 0770 {} +; "
                "find /var/lib/semaphore -type f "
                "-exec chmod 0660 {} +",
            ]
        )
        self.run_logged(
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
                'set -eu; '
                'probe="/var/lib/semaphore/.write-test"; '
                ': >"$probe"; rm -f "$probe"',
            ]
        )
        self.ok(
            "Хранилище Semaphore доступно для записи из контейнера"
        )

    def deploy_semaphore(self) -> None:
        self.stage("Сборка и запуск Semaphore")
        self.info("Получение базового образа Semaphore")
        self.run_logged(
            [
                "docker",
                "pull",
                f"semaphoreui/semaphore:{SEMAPHORE_VERSION}",
            ]
        )
        self.ok("Базовый образ Semaphore готов")

        self.run_raw(
            self.compose("stop", "runtime"),
            quiet=True,
            check=False,
        )
        self.repair_semaphore_storage()

        self.info(
            "Сборка infra-runtime с Semaphore, OpenTofu, "
            "Packer и Ansible"
        )
        self.run_logged(self.compose("build", "--pull", "runtime"))
        self.ok("Образ infra-runtime собран")

        self.info("Запуск контейнера infra-runtime")
        self.run_logged(
            self.compose("up", "-d", "--remove-orphans")
        )
        self.ok("Контейнер infra-runtime запущен")

    def semaphore_ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:3000/",
                timeout=5,
            ) as response:
                return 200 <= response.status < 400
        except (OSError, urllib.error.URLError):
            return False

    def append_to_log(self, argv: list[str]) -> None:
        with self.log_file.open("a", encoding="utf-8") as stream:
            subprocess.run(
                argv,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )

    def wait_semaphore(self) -> None:
        self.stage("Проверка Semaphore")
        for _ in range(60):
            if self.semaphore_ready():
                break
            time.sleep(2)

        if not self.semaphore_ready():
            self.append_to_log(self.compose("ps"))
            self.append_to_log(
                self.compose("logs", "--tail=100", "runtime")
            )
            raise InfraManagerError(
                "Semaphore Server не стал доступен"
            )

        time.sleep(5)
        running = self.run_raw(
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
                self.run_logged(argv)
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

    def install_local_commands(self) -> None:
        source_package = (
            REPO_ROOT
            / "scripts/infra-manager/infra_manager"
        )
        target_package = PYTHON_INSTALL_ROOT / "infra_manager"
        self.run_raw(
            [
                "install",
                "-d",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                str(PYTHON_INSTALL_ROOT),
            ]
        )
        if target_package.exists():
            shutil.rmtree(target_package)
        shutil.copytree(source_package, target_package)
        for path in target_package.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)

        for source, target in (
            (
                REPO_ROOT / "scripts/infra-manager/commands/status.sh",
                STATUS_COMMAND,
            ),
            (
                REPO_ROOT
                / "scripts/infra-manager/commands/pve-access-check.sh",
                ACCESS_CHECK_COMMAND,
            ),
            (
                REPO_ROOT
                / "scripts/infra-manager/commands/pve-lifecycle-test.sh",
                LIFECYCLE_TEST_COMMAND,
            ),
        ):
            self.install_file(source, target, "0755")

        self.run_raw(
            [
                "install",
                "-d",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                "/usr/local/bin",
            ]
        )
        for name, target in (
            ("infra-manager-status", STATUS_COMMAND),
            (
                "infra-manager-pve-access-check",
                ACCESS_CHECK_COMMAND,
            ),
            (
                "infra-manager-pve-lifecycle-test",
                LIFECYCLE_TEST_COMMAND,
            ),
        ):
            self.run_raw(
                [
                    "ln",
                    "-sfn",
                    str(target),
                    f"/usr/local/bin/{name}",
                ]
            )

    def report_result(self) -> None:
        address = self.run_raw(
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

        print(
            f"PVE API credential хранится: {PVE_API_ENV}"
        )

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
            # Локальные status/access команды сохраняют совместимые wrappers.
            self.install_local_commands()
            self.run_visible([str(STATUS_COMMAND)])

            self.report_result()
            return 0
        except (
            InfraManagerError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            self.error(str(exc))
            return 1


def run_setup() -> int:
    return Setup.from_environment().run()
