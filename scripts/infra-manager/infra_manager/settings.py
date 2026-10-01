"""Единые неизменяемые настройки и пути infra-manager."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Пути файлов и каталогов infra-manager."""

    repo_root: Path = Path(__file__).resolve().parents[3]
    config_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("INFRA_MANAGER_CONFIG_DIR", "/etc/infra-manager")
        )
    )
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("INFRA_MANAGER_DATA_DIR", "/var/lib/infra-manager")
        )
    )
    compose_dir: Path = Path("/opt/infra-manager/compose")
    python_install_root: Path = Path("/usr/local/lib/infra-manager")

    @property
    def asset_dir(self) -> Path:
        return self.repo_root / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose"

    @property
    def runtime_secret_dir(self) -> Path:
        return Path("/run/infra-manager/secrets")

    @property
    def openbao_materialized_marker(self) -> Path:
        return self.runtime_secret_dir / ".openbao-materialized"

    @property
    def ca_dir(self) -> Path:
        return self.config_dir / "ca"

    @property
    def ca_bundle(self) -> Path:
        return self.ca_dir / "ca-bundle.crt"

    @property
    def ssh_client_ca_public_key(self) -> Path:
        return self.ca_dir / "ssh-client-ca.pub"

    @property
    def ssh_host_ca_public_key(self) -> Path:
        return self.ca_dir / "ssh-host-ca.pub"

    @property
    def ansible_dir(self) -> Path:
        return self.config_dir / "ansible"

    @property
    def ansible_private_key(self) -> Path:
        return self.ansible_dir / "guest_ed25519"

    @property
    def ansible_public_key(self) -> Path:
        return self.ansible_dir / "guest_ed25519.pub"

    @property
    def pve_host_dir(self) -> Path:
        return Path(
            os.environ.get(
                "INFRA_PVE_HOST_DIR",
                "/mnt/pve-access/pve-host",
            )
        )

    @property
    def pve_host_private_key(self) -> Path:
        return self.pve_host_dir / "root_ed25519"

    @property
    def pve_host_known_hosts(self) -> Path:
        return self.pve_host_dir / "known_hosts"

    @property
    def semaphore_dir(self) -> Path:
        return self.data_dir / "semaphore"

    @property
    def opentofu_dir(self) -> Path:
        return self.data_dir / "opentofu"

    @property
    def opentofu_state_dir(self) -> Path:
        return self.opentofu_dir / "state"

    @property
    def opentofu_input(self) -> Path:
        return self.opentofu_dir / "guests.json"

    @property
    def status_file(self) -> Path:
        return self.config_dir / "status.yaml"

    @property
    def server_env(self) -> Path:
        return self.runtime_secret_dir / "semaphore-server.env"

    @property
    def pve_api_env(self) -> Path:
        return self.runtime_secret_dir / "pve-api.env"

    @property
    def admin_password_file(self) -> Path:
        return self.runtime_secret_dir / "initial-admin-password"

    @property
    def semaphore_api_token_file(self) -> Path:
        return self.runtime_secret_dir / "semaphore-api-token"

    @property
    def github_key(self) -> Path:
        return self.runtime_secret_dir / "github_proxmox_repo_ed25519"

    @property
    def github_key_copy(self) -> Path:
        return self.github_key

    @property
    def semaphore_project_id_file(self) -> Path:
        return self.data_dir / "semaphore-project-id"

    @property
    def status_command(self) -> Path:
        return Path("/usr/local/sbin/infra-manager-status")

    @property
    def access_check_command(self) -> Path:
        return Path("/usr/local/sbin/infra-manager-pve-access-check")

    @property
    def lifecycle_test_command(self) -> Path:
        return Path("/usr/local/sbin/infra-manager-pve-lifecycle-test")


@dataclass(frozen=True)
class Settings:
    """Имена, версии и значения по умолчанию infra-manager."""

    default_project_branch: str = "main"
    semaphore_url: str = "http://127.0.0.1:3000"
    project_name: str = "Proxmox Infrastructure"
    project_repo: str = "git@github.com:zsergeyru/proxmox.git"
    opentofu_env_name: str = "OpenTofu PVE"
    infra_manager_env_name: str = "Infra Manager"
    managed_pool: str = "managed"
    semaphore_version: str = "v2.18.30"
    runtime_version: str = "v1"
    opentofu_version: str = "1.12.6"
    packer_version: str = "1.15.4"
    bootstrap_managed_vmids: frozenset[int] = frozenset({910})

    def project_branch(self) -> str:
        """Вернуть выбранную Git-ветку проекта."""
        return os.environ.get(
            "INFRA_PROJECT_BRANCH",
            self.default_project_branch,
        )


PATHS = Paths()
SETTINGS = Settings()
