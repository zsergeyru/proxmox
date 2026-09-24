"""Подготовка Debian-хоста для infra-manager."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import InfraManagerError, command_runner
from .settings import PATHS

REPO_ROOT = PATHS.repo_root
CONFIG_DIR = PATHS.config_dir
SECRET_DIR = PATHS.secret_dir
CA_DIR = PATHS.ca_dir
DATA_DIR = PATHS.data_dir
SEMAPHORE_DIR = PATHS.semaphore_dir
OPENTOFU_DIR = PATHS.opentofu_dir
STATE_DIR = PATHS.opentofu_state_dir
COMPOSE_DIR = PATHS.compose_dir
PYTHON_INSTALL_ROOT = PATHS.python_install_root
STATUS_COMMAND = PATHS.status_command
ACCESS_CHECK_COMMAND = PATHS.access_check_command
LIFECYCLE_TEST_COMMAND = PATHS.lifecycle_test_command
PVE_API_ENV = PATHS.pve_api_env
CA_BUNDLE = PATHS.ca_bundle
ANSIBLE_DIR = PATHS.ansible_dir
ANSIBLE_PRIVATE_KEY = PATHS.ansible_private_key
ANSIBLE_PUBLIC_KEY = PATHS.ansible_public_key

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


class HostSetup:
    """Шаги подготовки ОС, Docker, постоянных путей и ключей."""

    @staticmethod
    def installed(package: str) -> bool:
        return (
            command_runner.run(
                ["dpkg", "-s", package],
                quiet=True,
                check=False,
            ).returncode
            == 0
        )

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

    def check_os(self) -> None:
        release = self.os_release()
        if release.get("ID") != "debian":
            raise InfraManagerError("Ожидается Debian")
        if release.get("VERSION_ID") != "13":
            raise InfraManagerError(
                f"Ожидается Debian 13, обнаружено "
                f"{release.get('VERSION_ID', '?')}"
            )
        architecture = command_runner.run(
            ["dpkg", "--print-architecture"],
            capture=True,
        ).stdout.strip()
        if architecture != "amd64":
            raise InfraManagerError(
                "Первая версия infra-manager рассчитана на amd64"
            )

    def prepare_directories(self) -> None:
        for path in (CONFIG_DIR, CA_DIR, DATA_DIR, COMPOSE_DIR):
            command_runner.run(
                [
                    "install", "-d", "-o", "root", "-g", "root",
                    "-m", "0755", str(path),
                ]
            )
        command_runner.run(
            [
                "install", "-d", "-o", "root", "-g", "root",
                "-m", "0700", str(SECRET_DIR),
            ]
        )
        command_runner.run(
            [
                "install", "-d", "-o", "1001", "-g", "0",
                "-m", "0770", str(SEMAPHORE_DIR),
            ]
        )
        for path in (OPENTOFU_DIR, STATE_DIR):
            command_runner.run(
                [
                    "install", "-d", "-o", "1001", "-g", "0",
                    "-m", "0750", str(path),
                ]
            )
        command_runner.run(["chown", "-R", "1001:0", str(SEMAPHORE_DIR)])
        command_runner.run(["chmod", "0770", str(SEMAPHORE_DIR)])

    def ensure_ansible_identity(self) -> None:
        """Создать или проверить постоянную SSH-идентичность Ansible."""
        self.stage("Проверка SSH-идентичности Ansible")
        command_runner.run(
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
            self.runner.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t", "ed25519",
                    "-N", "",
                    "-C", "infra-manager ansible guest",
                    "-f", str(ANSIBLE_PRIVATE_KEY),
                ]
            )

        derived = command_runner.run(
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
        self.runner.run(["apt-get", "update"])
        self.runner.run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *missing,
            ],
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        self.ok("Базовые пакеты infra-manager установлены")

    def install_docker(self) -> None:
        docker_ready = (
            command_runner.run(
                ["docker", "compose", "version"],
                quiet=True,
                check=False,
            ).returncode
            == 0
            if shutil.which("docker")
            else False
        )
        if docker_ready:
            self.runner.run(["systemctl", "enable", "--now", "docker"])
            self.ok("Docker Engine и Compose уже доступны")
            return

        for package in DOCKER_CONFLICT_PACKAGES:
            if self.installed(package):
                raise InfraManagerError(
                    f"Обнаружен конфликтующий пакет {package}. "
                    "Удалите его осознанно перед установкой Docker CE."
                )

        self.stage("Установка Docker Engine из официального репозитория")
        self.runner.run(["apt-get", "update"])
        command_runner.run(["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
        self.runner.run(
            [
                "curl",
                "-fsSL",
                "https://download.docker.com/linux/debian/gpg",
                "-o",
                "/etc/apt/keyrings/docker.asc",
            ]
        )
        command_runner.run(["chmod", "a+r", "/etc/apt/keyrings/docker.asc"])

        release = self.os_release()
        architecture = command_runner.run(
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

        self.runner.run(["apt-get", "update"])
        self.runner.run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                *DOCKER_PACKAGES,
            ],
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        self.runner.run(["systemctl", "enable", "--now", "docker"])
        self.runner.run(["docker", "version"])
        self.runner.run(["docker", "compose", "version"])
        self.ok("Docker Engine и Compose установлены")

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
                    command_runner.run(
                        ["cmp", "-s", str(staging), str(PVE_API_ENV)],
                        quiet=True,
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

    def install_local_commands(self) -> None:
        source_package = REPO_ROOT / "scripts/infra-manager/infra_manager"
        target_package = PYTHON_INSTALL_ROOT / "infra_manager"
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
                "/usr/local/bin",
            ]
        )
        for name, target in (
            ("infra-manager-status", STATUS_COMMAND),
            ("infra-manager-pve-access-check", ACCESS_CHECK_COMMAND),
            ("infra-manager-pve-lifecycle-test", LIFECYCLE_TEST_COMMAND),
        ):
            command_runner.run(
                [
                    "ln",
                    "-sfn",
                    str(target),
                    f"/usr/local/bin/{name}",
                ]
            )
